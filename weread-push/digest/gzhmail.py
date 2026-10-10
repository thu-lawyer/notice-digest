#!/usr/bin/env python3
"""gzhmail — 每日公众号新文章个性化邮件摘要。

数据源：we-mp-rss 引擎的 SQLite（只读）。LLM 排序：智谱 open.bigmodel.cn。
全部凭据从环境文件读取，脚本本身不含任何密钥。

用法：
  python3 gzhmail.py --dry-run   # 只打印，不发信
  python3 gzhmail.py             # 正式发信（默认由 systemd timer 08:15 调度）
"""
import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

BASE = Path(__file__).resolve().parent


def load_env(path):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def pick(candidates, cols):
    for c in candidates:
        if c in cols:
            return c
    return None


def open_db_ro(path):
    uri = "file:%s?mode=ro" % path
    return sqlite3.connect(uri, uri=True, timeout=10)


def find_article_schema(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    art = None
    for t in tables:
        if t.lower() == "articles":
            art = t
    if art is None:
        for t in tables:
            if "article" in t.lower():
                art = t
    if art is None:
        sys.exit("gzhmail: 未找到文章表，现有表=%s" % tables)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % art)]
    m = {
        "table": art,
        "title": pick(["title", "name"], cols),
        "url": pick(["url", "link", "content_url", "article_url"], cols),
        "mp": pick(["mp_id", "feed_id", "faker_id", "mp"], cols),
        "time": pick(["pub_time", "publish_time", "publish_at", "create_time",
                      "created_at", "create_at", "create_date", "updated_at"], cols),
        "id": pick(["id", "article_id"], cols),
    }
    missing = [k for k in ("title", "url", "mp") if not m[k]]
    if missing:
        sys.exit("gzhmail: 文章表缺字段 %s，列=%s" % (missing, cols))
    return m


def find_feed_names(conn):
    """返回 {feed主键: 显示名}；找不到就返回空 dict（回退用 mp_id 解码）。"""
    for t in ("feeds", "feed", "mps", "mp"):
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % t)]
        except sqlite3.Error:
            continue
        if not cols:
            continue
        name_c = pick(["name", "nickname", "title", "mp_name"], cols)
        id_c = pick(["id", "mp_id", "faker_id"], cols)
        if name_c and id_c:
            return {r[0]: r[1] for r in conn.execute(
                "SELECT %s, %s FROM %s" % (id_c, name_c, t))}
    return {}


def decode_mp_id(mp_id):
    """MP_WXS_<base64> 还原为可读标记；解不出就原样返回。"""
    if mp_id and mp_id.startswith("MP_WXS_"):
        raw = mp_id[len("MP_WXS_"):]
        try:
            raw += "=" * (-len(raw) % 4)
            return base64.b64decode(raw).decode("utf-8", "replace")
        except Exception:
            return mp_id
    return mp_id


def to_epoch(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        v = float(v)
        if v > 1e12:
            v /= 1000.0
        return v
    s = str(v).strip()
    if s.isdigit():
        return to_epoch(int(s))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 2], fmt).timestamp()
        except ValueError:
            continue
    return None


def fetch_recent(conn, m, names, lookback_hours):
    cols = ", ".join([m["id"], m["title"], m["url"], m["mp"], m["time"]])
    rows = conn.execute("SELECT %s FROM %s" % (cols, m["table"])).fetchall()
    cutoff = time.time() - lookback_hours * 3600
    out = []
    for aid, title, url, mp, ts in rows:
        ep = to_epoch(ts)
        if ep is None or ep < cutoff:
            continue
        src = names.get(mp) or decode_mp_id(mp)
        out.append({"id": str(aid), "title": title or "(无标题)", "url": url or "",
                    "mp": src, "ts": int(ep)})
    out.sort(key=lambda x: -x["ts"])
    return out


def load_state(path):
    try:
        return set(json.load(open(path, encoding="utf-8"))["sent"])
    except Exception:
        return set()


def save_state(path, sent_ids):
    keep = list(sent_ids)[-5000:]
    json.dump({"sent": keep, "ts": time.time()},
              open(path, "w", encoding="utf-8"), ensure_ascii=False)


def zhipu_rank(items, top_n, model):
    """LLM 排序：返回选中的 item 下标列表；失败返回 None。"""
    lines = ["%d. [%s] %s" % (i, it["mp"], it["title"])
             for i, it in enumerate(items)]
    sys_p = ("你是资讯助手。读者是清华大学法学院研究生，关注：法学学术与讲座、"
             "司法实务动态、AI与法律交叉、清华校园事务。")
    usr_p = ("下面是今日公众号新文章清单。选出对读者最重要的 %d 篇，"
             "优先：讲座/征稿/评选类通知、重要司法与立法动态、AI×法深度内容。"
             "只输出 JSON：{\"ids\": [编号,...]}，不要解释。\n%s"
             % (top_n, "\n".join(lines)))
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user", "content": usr_p}],
        "temperature": 0.2, "max_tokens": 8000,
    }).encode()
    req = urllib.request.Request(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ["ZHIPU_API_KEY"]})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.load(r)
        txt = str(data["choices"][0]["message"]["content"])
        ids = None
        m = re.search(r"\{[^{}]*\}", txt, re.S)
        if m:
            ids = json.loads(m.group(0)).get("ids")
        if not ids:
            ids = re.findall(r"\d+", txt)[:top_n]
        idx = [int(i) for i in ids if str(i).lstrip("-").isdigit()]
        if idx and 0 not in idx and all(1 <= i <= len(items) for i in idx):
            idx = [i - 1 for i in idx]  # 容错 1-based 编号
        idx = [i for i in idx if 0 <= i < len(items)][:top_n]
        if not idx:
            raise ValueError("空结果: %r" % txt[:80])
    except Exception as e:
        head = str(locals().get("txt", ""))[:100].replace("\n", " ")
        print("gzhmail: LLM 排序失败，回退关键词法：%s | resp=%s" % (e, head),
              file=sys.stderr)
        return None
    return idx


KEY_FALLBACK = ["讲座", "征稿", "研讨", "论坛", "法学", "司法", "立法",
                "最高法", "最高检", "AI", "人工智能", "清华", "招聘", "评奖"]


def fallback_rank(items, top_n):
    def score(it):
        return sum(1 for k in KEY_FALLBACK if k.lower() in it["title"].lower())
    idx = sorted(range(len(items)), key=lambda i: -score(items[i]))
    return idx[:top_n]


def render(items, picks, dry):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    head = "<h2>今日重点</h2><ol>" + "".join(
        '<li>【%s】<a href="%s">%s</a></li>' % (items[i]["mp"], items[i]["url"],
                                                items[i]["title"])
        for i in picks) + "</ol>"
    groups = {}
    for it in items:
        groups.setdefault(it["mp"], []).append(it)
    body = ["<h2>全部新文章（%d 篇，按公众号）</h2>" % len(items)]
    for g in sorted(groups, key=lambda g: g.encode("gbk", "ignore")):
        body.append("<h3>%s（%d）</h3><ul>" % (g, len(groups[g])))
        for it in groups[g]:
            t = datetime.fromtimestamp(it["ts"]).strftime("%m-%d %H:%M")
            body.append('<li>[%s] <a href="%s">%s</a></li>' % (t, it["url"],
                                                               it["title"]))
        body.append("</ul>")
    html = ("<div style='font-family:-apple-system,Segoe UI,sans-serif;"
            "max-width:680px'>"
            "<p style='color:#888'>生成于 %s%s</p>%s%s"
            "<p style='color:#888;font-size:12px'>gzhmail · 数据源 "
            "we-mp-rss 本地库存 · 排序 智谱GLM</p></div>"
            % (now, "（DRY-RUN，未发信）" if dry else "", head, "".join(body)))
    return html


def send_mail(html, subject):
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((os.environ["MAIL_FROM_NAME"],
                              os.environ["SMTP_USER"]))
    msg["To"] = os.environ["MAIL_TO"]
    import smtplib
    srv = smtplib.SMTP_SSL(os.environ["SMTP_HOST"],
                           int(os.environ["SMTP_PORT"]), timeout=30)
    srv.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
    srv.sendmail(os.environ["SMTP_USER"], [os.environ["MAIL_TO"]],
                 msg.as_string())
    srv.quit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_env(BASE / "gzhmail.env")
    need = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
            "MAIL_TO", "MAIL_FROM_NAME", "ZHIPU_API_KEY", "LLM_MODEL"]
    miss = [k for k in need if not os.environ.get(k)]
    if miss:
        sys.exit("gzhmail.env 缺配置：%s" % miss)
    db = os.environ.get("GZH_DB", "/opt/weread-push/we-mp-rss/data/db.db")
    state_p = os.environ.get("STATE", str(BASE / "state.json"))
    lookback = float(os.environ.get("LOOKBACK_HOURS", "26"))
    top_n = int(os.environ.get("TOP_N", "10"))

    conn = open_db_ro(db)
    m = find_article_schema(conn)
    names = find_feed_names(conn)
    items = fetch_recent(conn, m, names, lookback)
    sent = load_state(state_p)
    fresh = [it for it in items
             if hashlib.md5(it["url"].encode()).hexdigest() not in sent]
    print("gzhmail: 窗口内 %d 篇，未推送 %d 篇" % (len(items), len(fresh)))
    if not fresh:
        print("gzhmail: 无新文章，不发信")
        return
    picks = zhipu_rank(fresh, min(top_n, len(fresh)), os.environ["LLM_MODEL"])
    if picks is None:
        picks = fallback_rank(fresh, min(top_n, len(fresh)))
    html = render(fresh, picks, args.dry_run)
    if args.dry_run:
        print("重点 %d 篇：" % len(picks))
        for i in picks:
            print("  [%s] %s" % (fresh[i]["mp"], fresh[i]["title"]))
        return
    today = datetime.now().strftime("%m-%d")
    send_mail(html, "公众号日报 %s · %d 篇新文章" % (today, len(fresh)))
    for it in fresh:
        sent.add(hashlib.md5(it["url"].encode()).hexdigest())
    save_state(state_p, sent)
    print("gzhmail: 已发信 %d 篇（重点 %d）" % (len(fresh), len(picks)))


if __name__ == "__main__":
    main()
