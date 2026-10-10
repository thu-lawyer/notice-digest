"""gzh_source — 公众号新文章数据源（we-mp-rss 只读 SQLite + 智谱 LLM 排序）。

自服务器独立脚本 gzhmail.py 移植（参考 .ref/gzhmail.py），只保留「取增量、
排序、记账」三件事；发信与渲染统一归 notice-digest，两封邮件由此合并为一封。

硬约定：
- 只用标准库（sqlite3/base64/hashlib/json/re/time/urllib），不引第三方依赖；
- 路径与凭据一律经 Config 传入（ND_GZH_DB / ND_GZH_STATE / ND_ZHIPU_API_KEY /
  ND_LLM_MODEL / ND_GZH_LOOKBACK / ND_GZH_TOP），模块内**不读环境变量**；
- 数据库只读打开（``file:...?mode=ro``），绝不写 we-mp-rss 的库；
- 记账（state-gzh.json）由调用方在**成功发信后**调用 :func:`mark_sent`，
  与 notice 侧的发送台账互相独立、互不影响幂等闸门。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

ZHIPU_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
LLM_TIMEOUT_SECONDS = 180

#: LLM 排序失败时的关键词兜底（与 gzhmail 同表，不引入新口径）
KEY_FALLBACK = [
    "讲座", "征稿", "研讨", "论坛", "法学", "司法", "立法",
    "最高法", "最高检", "AI", "人工智能", "清华", "招聘", "评奖",
]


def article_key(url: str) -> str:
    """文章身份 = URL 的 md5。与 gzhmail 已发送台账（state-gzh.json）同口径。"""
    return hashlib.md5((url or "").encode("utf-8")).hexdigest()


def normalize_title(title: str) -> str:
    """跨源标题去重键：去掉空白与常见中英标点后转小写。

    notice 与公众号同标题时通知优先，本函数只提供归一化比较键。
    """
    text = re.sub(
        r"[\s\u3000\[\]【】（）()《》<>「」『』·：:,，。.、;；！!？?\-—_|｜\"'“”]+",
        "",
        title or "",
    )
    return text.lower()


def load_state(path) -> set:
    """读已发送文章 key 集合；文件缺失/损坏一律视为空集合（不抛错）。"""
    try:
        with open(str(path), encoding="utf-8") as fh:
            return set(json.load(fh).get("sent") or [])
    except Exception:
        return set()


def save_state(path, sent_ids) -> None:
    """原子化写台账：只保留最近 5000 条，排序后写入（内容确定，便于核对）。"""
    keep = sorted(sent_ids)[-5000:]
    target = Path(path)
    if str(target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"sent": keep, "ts": time.time()}, ensure_ascii=False)
    target.write_text(payload, encoding="utf-8")


def _pick(candidates, cols):
    for candidate in candidates:
        if candidate in cols:
            return candidate
    return None


def state_path(cfg) -> Path:
    """台账路径：显式 ND_GZH_STATE 优先，否则落在 gzh_db 同目录 state-gzh.json。"""
    if getattr(cfg, "gzh_state", ""):
        return Path(cfg.gzh_state)
    if getattr(cfg, "gzh_db", ""):
        return Path(cfg.gzh_db).parent / "state-gzh.json"
    raise RuntimeError("gzh_source: 未配置 ND_GZH_DB / ND_GZH_STATE")


def open_db_ro(path) -> sqlite3.Connection:
    """只读打开 we-mp-rss 的 SQLite；路径不存在时抛 FileNotFoundError。"""
    uri = "file:%s?mode=ro" % Path(path).resolve()
    return sqlite3.connect(uri, uri=True, timeout=10)


def find_article_schema(conn) -> dict:
    """探测文章表与列名（we-mp-rss 各版本字段名不同，按候选表逐个试）。"""
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    art = None
    for table in tables:
        if table.lower() == "articles":
            art = table
    if art is None:
        for table in tables:
            if "article" in table.lower():
                art = table
    if art is None:
        raise RuntimeError("gzh_source: 未找到文章表，现有表=%s" % tables)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(%s)" % art)]
    mapping = {
        "table": art,
        "title": _pick(["title", "name"], cols),
        "url": _pick(["url", "link", "content_url", "article_url"], cols),
        "mp": _pick(["mp_id", "feed_id", "faker_id", "mp"], cols),
        "time": _pick([
            "pub_time", "publish_time", "publish_at", "create_time",
            "created_at", "create_at", "create_date", "updated_at",
        ], cols),
        "id": _pick(["id", "article_id"], cols),
    }
    missing = [key for key in ("title", "url", "mp") if not mapping[key]]
    if missing:
        raise RuntimeError("gzh_source: 文章表缺字段 %s，列=%s" % (missing, cols))
    return mapping


def find_feed_names(conn) -> dict:
    """返回 {feed 主键: 显示名}；找不到就返回空 dict（回退用 mp_id 解码）。"""
    for table in ("feeds", "feed", "mps", "mp"):
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]
        except sqlite3.Error:
            continue
        if not cols:
            continue
        name_col = _pick(["name", "nickname", "title", "mp_name"], cols)
        id_col = _pick(["id", "mp_id", "faker_id"], cols)
        if name_col and id_col:
            return {
                row[0]: row[1]
                for row in conn.execute("SELECT %s, %s FROM %s" % (id_col, name_col, table))
            }
    return {}


def decode_mp_id(mp_id):
    """MP_WXS_<base64> 还原为可读公众号标记；解不出就原样返回。"""
    if mp_id and mp_id.startswith("MP_WXS_"):
        raw = mp_id[len("MP_WXS_"):]
        try:
            raw += "=" * (-len(raw) % 4)
            return base64.b64decode(raw).decode("utf-8", "replace")
        except Exception:
            return mp_id
    return mp_id


def to_epoch(value):
    """把库里的时间字段（秒/毫秒/字符串）统一成 epoch 秒；解不出返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e12:  # 毫秒时间戳
            number /= 1000.0
        return number
    text = str(value).strip()
    if text.isdigit():
        return to_epoch(int(text))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt).timestamp()
        except ValueError:
            continue
    return None


def fetch_recent(conn, mapping, feed_names, lookback_hours, now_ts=None) -> list:
    """窗口内文章，按发布时间倒序。纯函数（now_ts 可注入，便于测试）。"""
    cols = ", ".join([
        mapping["id"], mapping["title"], mapping["url"], mapping["mp"], mapping["time"],
    ])
    rows = conn.execute("SELECT %s FROM %s" % (cols, mapping["table"])).fetchall()
    now = float(now_ts) if now_ts is not None else time.time()
    cutoff = now - lookback_hours * 3600
    out = []
    for aid, title, url, mp, ts in rows:
        epoch = to_epoch(ts)
        if epoch is None or epoch < cutoff:
            continue
        source = feed_names.get(mp) or decode_mp_id(mp)
        out.append({
            "id": str(aid),
            "title": title or "(无标题)",
            "url": url or "",
            "mp": source,
            "ts": int(epoch),
        })
    out.sort(key=lambda entry: -entry["ts"])
    return out


def collect(cfg) -> list:
    """窗口内未发送的新文章（已按 URL 去重、排除台账）。数据库不可用时返回 []。"""
    db_path = str(cfg.gzh_db or "")
    if not db_path or not Path(db_path).exists():
        return []
    conn = open_db_ro(db_path)
    try:
        mapping = find_article_schema(conn)
        feed_names = find_feed_names(conn)
        items = fetch_recent(conn, mapping, feed_names, cfg.gzh_lookback)
    finally:
        conn.close()
    sent = load_state(state_path(cfg))
    fresh, seen = [], set()
    for item in items:
        key = article_key(item["url"])
        if not item["url"] or key in sent or key in seen:
            continue
        seen.add(key)
        item["key"] = key
        fresh.append(item)
    return fresh


def zhipu_rank(items, top_n, api_key, model):
    """智谱 GLM 排序：返回选中的 item 下标列表；任何失败返回 None（调用方回退）。"""
    lines = ["%d. [%s] %s" % (index, item["mp"], item["title"])
             for index, item in enumerate(items)]
    sys_prompt = (
        "你是资讯助手。读者是清华大学法学院研究生，关注：法学学术与讲座、"
        "司法实务动态、AI与法律交叉、清华校园事务。"
    )
    usr_prompt = (
        "下面是今日公众号新文章清单。选出对读者最重要的 %d 篇，"
        "优先：讲座/征稿/评选类通知、重要司法与立法动态、AI×法深度内容。"
        "只输出 JSON：{\"ids\": [编号,...]}，不要解释。\n%s"
        % (top_n, "\n".join(lines))
    )
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": usr_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 8000,
    }).encode()
    req = urllib.request.Request(
        ZHIPU_ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_SECONDS) as resp:
            data = json.load(resp)
        text = str(data["choices"][0]["message"]["content"])
        ids = None
        match = re.search(r"\{[^{}]*\}", text, re.S)
        if match:
            ids = json.loads(match.group(0)).get("ids")
        if not ids:
            ids = re.findall(r"\d+", text)[:top_n]
        idx = [int(value) for value in ids if str(value).lstrip("-").isdigit()]
        # 容错 1-based 编号：模型常按清单序号（从 1 起）回答
        if idx and 0 not in idx and all(1 <= value <= len(items) for value in idx):
            idx = [value - 1 for value in idx]
        idx = [value for value in idx if 0 <= value < len(items)][:top_n]
        if not idx:
            raise ValueError("空结果: %r" % text[:80])
    except Exception as exc:  # 网络/解析/越界一律回退关键词法
        head = ""
        try:
            head = str(text)[:100].replace("\n", " ")  # noqa: F821 - 仅在解析成功后存在
        except Exception:
            head = ""
        print(
            "gzh_source: LLM 排序失败，回退关键词法：%s | resp=%s" % (exc, head),
            file=__import__("sys").stderr,
        )
        return None
    return idx


def fallback_rank(items, top_n):
    """关键词计分兜底排序：返回下标列表（稳定、无外部依赖）。"""
    def score(item):
        title = (item.get("title") or "").lower()
        return sum(1 for keyword in KEY_FALLBACK if keyword.lower() in title)

    ranked = sorted(range(len(items)), key=lambda i: (-score(items[i]), i))
    return ranked[:top_n]


def rank(items, top_n, cfg) -> list:
    """统一入口：LLM 可用则用 LLM，否则关键词兜底；返回去重后的下标列表。"""
    count = max(0, min(int(top_n), len(items)))
    if count == 0:
        return []
    idx = None
    if cfg.gzh_zhipu_api_key and cfg.gzh_llm_model:
        idx = zhipu_rank(items, count, cfg.gzh_zhipu_api_key, cfg.gzh_llm_model)
    if idx is None:
        idx = fallback_rank(items, count)
    ordered, seen = [], set()
    for value in idx:
        if 0 <= value < len(items) and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered[:count]


def mark_sent(cfg, items) -> int:
    """把文章 key 写入台账（**仅在整封邮件成功发出后调用**）。返回写入数量。"""
    if not items:
        return 0
    sent = load_state(state_path(cfg))
    before = len(sent)
    for item in items:
        sent.add(item.get("key") or article_key(item.get("url") or ""))
    save_state(state_path(cfg), sent)
    return len(sent) - before
