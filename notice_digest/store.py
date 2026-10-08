"""SQLite 持久化层。

设计要点：
  * items 以 id 为主键 upsert，天然幂等；
  * 列表条目的 9 个字段与详情字段分开存列，详情补全只填 detail_* 列；
  * weights / feedback 支撑在线学习；feedback 以 (item_id, kind) 唯一，重复反馈只计一次。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ_SHANGHAI = timezone(timedelta(hours=8))

#: 需要详情补全的 intent_group
ENRICH_INTENT_GROUPS = ("activity",)

#: 需要详情补全的分类
ENRICH_CATEGORIES = ("讲座活动", "文体活动", "社团公益", "校园动态")


def needs_enrich(item: dict) -> bool:
    """是否值得抓详情：activity 或关键分类；``detail_status=complete`` 的不再重抓。

    模块级函数（``enrich`` 直接 import 它）；同时以 :meth:`Store.needs_enrich`
    静态方法暴露，两种调用方式都可用。
    """
    if (item.get("detail_status") or "").strip().lower() == "complete":
        return False
    if (item.get("intent_group") or "") in ENRICH_INTENT_GROUPS:
        return True
    return (item.get("category") or "") in ENRICH_CATEGORIES


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id                TEXT PRIMARY KEY,
    source_id         TEXT,
    source_name       TEXT,
    title             TEXT NOT NULL,
    published_at      TEXT,
    published_ts      REAL,
    url               TEXT,
    category          TEXT,
    intent_group      TEXT,
    english_title     TEXT,
    english_source    TEXT,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    detail_status     TEXT,
    body_status       TEXT,
    detail_json       TEXT,
    detail_fetched_at TEXT,
    enrich_attempts   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_ts DESC);
CREATE INDEX IF NOT EXISTS idx_items_detail    ON items(detail_status);

CREATE TABLE IF NOT EXISTS sends (
    date      TEXT PRIMARY KEY,
    subject   TEXT,
    n_items   INTEGER,
    sent_at   TEXT NOT NULL
);

-- 投递尝试台账：先落 attempt 行抢占发送权，发信成功后升级为 sent。
-- 进程在"落闸之后、发信之前"崩掉，就会留下 status='attempt' 的行 ⇒ 崩溃窗口可检。
CREATE TABLE IF NOT EXISTS send_attempts (
    date        TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    subject     TEXT,
    n_items     INTEGER,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    sent_at     TEXT,
    PRIMARY KEY (date, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_send_attempts_date ON send_attempts(date DESC, started_at DESC);

CREATE TABLE IF NOT EXISTS weights (
    feature    TEXT PRIMARY KEY,
    value      REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    item_id  TEXT NOT NULL,
    kind     TEXT NOT NULL,
    value    REAL NOT NULL,
    ts       TEXT NOT NULL,
    PRIMARY KEY (item_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_feedback_ts ON feedback(ts DESC);
"""


def _as_timestamp(value) -> float | None:
    """把 date/datetime/ISO 文本/Unix 秒统一成 Unix 秒；无法解析返回 None。

    复用 :func:`parse_published_at` 的一套口径（尾部 Z 当 UTC、朴素时间当 Asia/Shanghai、
    仅日期取当天 00:00），避免窗口比较出现第二套时间语义。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = value.isoformat() if hasattr(value, "isoformat") else str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    return parse_published_at(text)


def parse_published_at(text: str | None) -> float | None:
    """把 published_at 一律按 Asia/Shanghai 解析成 Unix 秒。

    站点返回形如 ``2026-10-08T19:24:29+08:00``；不带时区的按东八区补。
    解析失败返回 None（绝不抛异常打断入库）。
    """
    if not text or not isinstance(text, str):
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_SHANGHAI)
    return dt.timestamp()


def now_shanghai() -> datetime:
    return datetime.now(TZ_SHANGHAI)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")

    # ------------------------------------------------------------------ 基础
    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Store":
        self.init_schema()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------------- 条目写
    def upsert_items(self, items: list[dict], *, skip_sink: dict | None = None) -> tuple[int, int]:
        """按 id 主键写入。返回 (新增数, 命中已知数)。

        ``skip_sink``（可选，D-3）：传入 dict 时，因缺少必需键被跳过的条目按原因
        计数写入其中（``not_dict`` / ``missing_id``）。缺 ``id`` 的列表项意味着上游
        列表结构漂移（如 ``id`` 改名），调用方必须据此在机器可读输出里留下痕迹，
        不得静默丢弃 —— 否则「日报为空」与「今天零新增」外部不可区分。
        """
        if not items:
            return (0, 0)
        self.init_schema()
        stamp = now_shanghai().isoformat()
        inserted = known = 0
        for raw in items:
            if not isinstance(raw, dict):
                if skip_sink is not None:
                    skip_sink["not_dict"] = skip_sink.get("not_dict", 0) + 1
                continue
            item_id = raw.get("id")
            if not item_id:
                if skip_sink is not None:
                    skip_sink["missing_id"] = skip_sink.get("missing_id", 0) + 1
                continue
            item_id = str(item_id)
            cur = self.conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,))
            exists = cur.fetchone() is not None

            english = raw.get("english") or {}
            if not isinstance(english, dict):
                english = {}
            published_ts = parse_published_at(raw.get("published_at"))

            if exists:
                known += 1
                self.conn.execute(
                    """
                    UPDATE items SET
                        source_id = COALESCE(?, source_id),
                        source_name = COALESCE(?, source_name),
                        title = COALESCE(?, title),
                        published_at = COALESCE(?, published_at),
                        published_ts = COALESCE(?, published_ts),
                        url = COALESCE(?, url),
                        category = COALESCE(?, category),
                        intent_group = COALESCE(?, intent_group),
                        english_title = COALESCE(?, english_title),
                        english_source = COALESCE(?, english_source),
                        last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        raw.get("source_id"),
                        raw.get("source_name"),
                        raw.get("title"),
                        raw.get("published_at"),
                        published_ts,
                        raw.get("url"),
                        raw.get("category"),
                        raw.get("intent_group"),
                        english.get("title"),
                        english.get("source_name"),
                        stamp,
                        item_id,
                    ),
                )
            else:
                inserted += 1
                self.conn.execute(
                    """
                    INSERT INTO items (
                        id, source_id, source_name, title, published_at, published_ts,
                        url, category, intent_group, english_title, english_source,
                        first_seen_at, last_seen_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item_id,
                        raw.get("source_id"),
                        raw.get("source_name"),
                        raw.get("title") or "",
                        raw.get("published_at"),
                        published_ts,
                        raw.get("url"),
                        raw.get("category"),
                        raw.get("intent_group"),
                        english.get("title"),
                        english.get("source_name"),
                        stamp,
                        stamp,
                    ),
                )
        self.conn.commit()
        return (inserted, known)

    def known_ids(self) -> set[str]:
        self.init_schema()
        return {row[0] for row in self.conn.execute("SELECT id FROM items")}

    def get_item(self, item_id: str) -> dict | None:
        self.init_schema()
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_item(row)

    def _row_to_item(self, row: sqlite3.Row) -> dict:
        item = {
            "id": row["id"],
            "source_id": row["source_id"],
            "source_name": row["source_name"],
            "title": row["title"],
            "published_at": row["published_at"],
            "url": row["url"],
            "category": row["category"],
            "intent_group": row["intent_group"],
            "english": {
                "title": row["english_title"],
                "source_name": row["english_source"],
            },
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "detail_status": row["detail_status"],
            "body_status": row["body_status"],
            "detail_fetched_at": row["detail_fetched_at"],
        }
        blob = row["detail_json"]
        if blob:
            try:
                detail = json.loads(blob)
            except (ValueError, TypeError):
                detail = None
            if isinstance(detail, dict):
                item["detail"] = detail
        return item

    def all_items(self, limit: int | None = None) -> list[dict]:
        self.init_schema()
        sql = "SELECT * FROM items ORDER BY published_ts DESC, id DESC"
        params: tuple = ()
        if limit:
            sql += " LIMIT ?"
            params = (int(limit),)
        return [self._row_to_item(r) for r in self.conn.execute(sql, params)]

    # ---------------------------------------------------------------- 详情写
    def update_detail(self, item_id: str, detail: dict) -> None:
        """写入详情补全结果（原样保存 JSON，便于后续重新解析）。"""
        self.init_schema()
        detail = detail or {}
        self.conn.execute(
            """
            UPDATE items SET
                detail_json = ?,
                detail_status = ?,
                body_status = ?,
                detail_fetched_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(detail, ensure_ascii=False),
                detail.get("detail_status"),
                detail.get("body_status"),
                now_shanghai().isoformat(),
                str(item_id),
            ),
        )
        self.conn.commit()

    def touch_enrich_attempt(self, item_id: str) -> None:
        self.conn.execute(
            "UPDATE items SET enrich_attempts = enrich_attempts + 1 WHERE id = ?",
            (str(item_id),),
        )
        self.conn.commit()

    @staticmethod
    def needs_enrich(item: dict) -> bool:
        """是否值得抓详情：activity 或关键分类；detail_status=complete 的不再重抓。"""
        return needs_enrich(item)

    def pending_enrich(self, limit: int) -> list[dict]:
        """列出待补全条目（最近发布优先）。"""
        self.init_schema()
        rows = self.conn.execute(
            """
            SELECT * FROM items
            WHERE (detail_status IS NULL OR lower(detail_status) != 'complete')
              AND (
                    intent_group IN ({})
                 OR category IN ({})
              )
            ORDER BY published_ts DESC, id DESC
            LIMIT ?
            """.format(
                ",".join("?" * len(ENRICH_INTENT_GROUPS)),
                ",".join("?" * len(ENRICH_CATEGORIES)),
            ),
            (*ENRICH_INTENT_GROUPS, *ENRICH_CATEGORIES, int(limit)),
        ).fetchall()
        return [self._row_to_item(r) for r in rows]

    def stats(self) -> dict:
        self.init_schema()
        total = self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        with_detail = self.conn.execute(
            "SELECT COUNT(*) FROM items WHERE detail_json IS NOT NULL"
        ).fetchone()[0]
        complete = self.conn.execute(
            "SELECT COUNT(*) FROM items WHERE lower(COALESCE(detail_status,'')) = 'complete'"
        ).fetchone()[0]
        pending = self.conn.execute(
            """
            SELECT COUNT(*) FROM items
            WHERE (detail_status IS NULL OR lower(detail_status) != 'complete')
              AND (intent_group IN ({}) OR category IN ({}))
            """.format(
                ",".join("?" * len(ENRICH_INTENT_GROUPS)),
                ",".join("?" * len(ENRICH_CATEGORIES)),
            ),
            (*ENRICH_INTENT_GROUPS, *ENRICH_CATEGORIES),
        ).fetchone()[0]
        by_category = {
            row[0] or "(未分类)": row[1]
            for row in self.conn.execute(
                "SELECT category, COUNT(*) FROM items GROUP BY category ORDER BY 2 DESC"
            )
        }
        n_weights = self.conn.execute("SELECT COUNT(*) FROM weights").fetchone()[0]
        n_feedback = self.conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        latest = self.conn.execute("SELECT MAX(published_ts) FROM items").fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "total_items": total,
            "with_detail": with_detail,
            "detail_complete": complete,
            "pending_enrich": pending,
            "by_category": by_category,
            "weights": n_weights,
            "feedback": n_feedback,
            "latest_published_ts": latest,
        }

    # ---------------------------------------------------------------- 发送记录
    def record_send(self, date: str, subject: str, n_items: int) -> None:
        self.init_schema()
        self.conn.execute(
            """
            INSERT INTO sends (date, subject, n_items, sent_at) VALUES (?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
                subject = excluded.subject,
                n_items = excluded.n_items,
                sent_at = excluded.sent_at
            """,
            (str(date), str(subject), int(n_items), now_shanghai().isoformat()),
        )
        self.conn.commit()

    def last_send(self) -> dict | None:
        self.init_schema()
        row = self.conn.execute(
            "SELECT * FROM sends ORDER BY date DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------- 投递闸门 / 崩溃窗口
    def begin_send(self, date: str, subject: str, fingerprint: str) -> bool:
        """发信前抢占本窗口的发送权，并把 attempt 行先落盘。

        返回 ``True``  ⇒ 本轮拿到发送权，调用方可以发信，成功后必须 :meth:`mark_sent`；
        返回 ``False`` ⇒ 同 ``(date, fingerprint)`` 已经 ``sent``，本次不发信（幂等闸门）。

        为什么先落 attempt 行：投递只有"发信"这一步会真的动外部世界，把状态写在它之前，
        进程若在发信与记账之间死掉，下一轮能用 :meth:`get_send` 看到 ``status='attempt'``
        的遗留行 ⇒ 崩溃窗口可检、可人工补记，而不是"什么都没发生"。
        """
        self.init_schema()
        day, fp = str(date), str(fingerprint)
        row = self.conn.execute(
            "SELECT status FROM send_attempts WHERE date=? AND fingerprint=?", (day, fp)
        ).fetchone()
        if row is not None and str(row["status"]) == "sent":
            return False
        self.conn.execute(
            """
            INSERT INTO send_attempts
                (date, fingerprint, subject, n_items, status, started_at, sent_at)
            VALUES (?,?,?,NULL,'attempt',?,NULL)
            ON CONFLICT(date, fingerprint) DO UPDATE SET
                subject    = excluded.subject,
                status     = 'attempt',
                started_at = excluded.started_at
            """,
            (day, fp, str(subject), now_shanghai().isoformat()),
        )
        self.conn.commit()
        return True

    def mark_sent(self, date: str, n_items: int, fingerprint: str, sent_at: str | None = None) -> None:
        """发信成功后把 attempt 行升级为 ``sent``，并同步维护兼容用的 sends 台账。"""
        self.init_schema()
        day, fp = str(date), str(fingerprint)
        stamp = str(sent_at) if sent_at else now_shanghai().isoformat()
        self.conn.execute(
            """
            INSERT INTO send_attempts
                (date, fingerprint, subject, n_items, status, started_at, sent_at)
            VALUES (?,?,NULL,?,'sent',?,?)
            ON CONFLICT(date, fingerprint) DO UPDATE SET
                n_items = excluded.n_items,
                status  = 'sent',
                sent_at = excluded.sent_at
            """,
            (day, fp, int(n_items), stamp, stamp),
        )
        row = self.conn.execute(
            "SELECT subject FROM send_attempts WHERE date=? AND fingerprint=?", (day, fp)
        ).fetchone()
        subject = str(row["subject"]) if row is not None and row["subject"] is not None else ""
        self.conn.execute(
            """
            INSERT INTO sends (date, subject, n_items, sent_at) VALUES (?,?,?,?)
            ON CONFLICT(date) DO UPDATE SET
                subject = excluded.subject,
                n_items = excluded.n_items,
                sent_at = excluded.sent_at
            """,
            (day, subject, int(n_items), stamp),
        )
        self.conn.commit()

    def get_send(self, date: str) -> dict | None:
        """查某日的投递状态：``None``=当天无任何投递记录；含 attempts 明细。

        ``status == 'attempt'`` 表示"抢了发送权但没有发完"（崩溃窗口）；
        ``pending_attempt`` 为 True 时同样提示存在未收尾的尝试。
        """
        self.init_schema()
        day = str(date)
        rows = [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM send_attempts WHERE date=? ORDER BY started_at DESC, fingerprint DESC",
                (day,),
            )
        ]
        legacy = self.conn.execute("SELECT * FROM sends WHERE date=?", (day,)).fetchone()
        if not rows and legacy is None:
            return None
        out: dict = dict(legacy) if legacy is not None else {}
        out["date"] = day
        out["attempts"] = rows
        if rows:
            newest = rows[0]
            for key in ("subject", "n_items", "sent_at"):
                if out.get(key) is None:
                    out[key] = newest.get(key)
            out["status"] = newest.get("status")
            out["fingerprint"] = newest.get("fingerprint")
            out["sent"] = any(r.get("status") == "sent" for r in rows)
            out["pending_attempt"] = any(r.get("status") == "attempt" for r in rows)
        else:
            out["status"] = "sent"  # 只有 Phase A 风格的 sends 行：视为已投递
            out["fingerprint"] = None
            out["sent"] = True
            out["pending_attempt"] = False
        return out

    def last_sent_at(self) -> str | None:
        """最近一次"成功投递"的时间（ISO 文本），从未投递过则 None。

        attempt 行不算：抢到权但没发完的那次不能推进投递窗口，否则崩溃窗口里的条目
        会被永久跳过。
        """
        self.init_schema()
        row = self.conn.execute(
            "SELECT sent_at FROM send_attempts WHERE status='sent' AND sent_at IS NOT NULL "
            "ORDER BY sent_at DESC LIMIT 1"
        ).fetchone()
        if row is not None and row["sent_at"]:
            return str(row["sent_at"])
        row = self.conn.execute(
            "SELECT sent_at FROM sends WHERE sent_at IS NOT NULL ORDER BY sent_at DESC LIMIT 1"
        ).fetchone()
        return str(row["sent_at"]) if row is not None and row["sent_at"] else None

    # ---------------------------------------------------------------- 投递窗口
    def items_published_after(self, since, limit: int | None = None) -> list[dict]:
        """取"上次投递之后"发布的条目，顺序稳定（published_ts DESC, id DESC）。

        ``since`` 可为 date/datetime/ISO 文本/Unix 秒，``None`` 表示不限（首次投递）。

        ``published_ts`` 解析不出来的条目（NULL）用 ``first_seen_at``（条目**首次**被看到的
        时刻，重复 upsert 不改写它，见 ``upsert_items``）当单调游标，语义有两条并列：

        ① **跨投递日不重复**：``first_seen_at`` 早于 ``last_sent_at`` 的 NULL 行不再进窗口 ——
           这条关掉 t13-F2 的"无上界重投"（原实现 ``published_ts IS NULL OR ...`` 让 NULL 行
           永远在窗口里，每天都再随信送出）。
        ② **同日仍重入**：``first_seen_at`` 落在当日的 NULL 行照旧进窗口 —— 该行同日重跑时
           正文与上一轮完全相同，由 F1 的**稳定内容指纹**在投递闸门处拦下（disp=0，不重复发信）；
           这条同时是 ``tests/test_render.py`` 既有断言（同日第 2 轮 n_items==1 + 闸门命中）
           所固定下来的行为，属 outOfScope 冻结项，不得为"更干净"而改掉它。

        即：NULL 行同日最多**发信**一次（靠闸门），跨日不再出现（靠游标）；残余面是
        "同日后续若另有新条目而重算正文，该 NULL 行会再随那封信出现一次"。
        只有 ``first_seen_at`` 也解析不出来时才无条件保留（``COALESCE`` 兜底为大值），
        这种极端情况下若仍重复投递，同样由投递指纹那道闸门拦下。

        投递窗口本身在 ``since is None``（从未成功投递）时不设上界，避免首投失败就静默丢条目。
        """
        self.init_schema()
        ts = _as_timestamp(since)
        sql = "SELECT * FROM items"
        params: list = []
        if ts is not None:
            # NULL 行的边界换算成儒略日（与 SQLite julianday() 同纪元：1970-01-01T00:00Z
            # == 2440587.5）；两列都是文本，直接比较字符串跨不过可变长度的小数秒。
            boundary_jd = float(ts) / 86400.0 + 2440587.5
            # 当日 0 点（Asia/Shanghai）的儒略日：NULL 行在"首次出现的那一天"内仍然入窗（同日重入，
            # 由稳定指纹闸门拦重复发信），跨日则只认 first_seen_at > last_sent_at 这条游标。
            day0 = now_shanghai().replace(hour=0, minute=0, second=0, microsecond=0)
            day_start_jd = day0.timestamp() / 86400.0 + 2440587.5
            sql += (
                " WHERE (published_ts IS NOT NULL AND published_ts > ?)"
                " OR (published_ts IS NULL AND ("
                "COALESCE(julianday(first_seen_at), 1e18) > ?"
                " OR COALESCE(julianday(first_seen_at), 1e18) >= ?))"
            )
            params.append(float(ts))
            params.append(boundary_jd)
            params.append(day_start_jd)
        sql += " ORDER BY published_ts DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._row_to_item(r) for r in self.conn.execute(sql, params)]

    def items_not_yet_sent(self, limit: int | None = None) -> list[dict]:
        """按最近一次成功投递的时间开窗（cmd_send 用它装配本期的条目列表）。"""
        self.init_schema()
        return self.items_published_after(self.last_sent_at(), limit=limit)

    # ---------------------------------------------------------------- 权重
    def get_weights(self) -> dict:
        self.init_schema()
        return {
            row[0]: float(row[1])
            for row in self.conn.execute("SELECT feature, value FROM weights")
        }

    def put_weights(self, w: dict) -> None:
        self.init_schema()
        stamp = now_shanghai().isoformat()
        rows = [(str(k), float(v), stamp) for k, v in (w or {}).items()]
        if not rows:
            return
        self.conn.executemany(
            """
            INSERT INTO weights (feature, value, updated_at) VALUES (?,?,?)
            ON CONFLICT(feature) DO UPDATE SET
                value = excluded.value, updated_at = excluded.updated_at
            """,
            rows,
        )
        self.conn.commit()

    # ---------------------------------------------------------------- 反馈
    def record_feedback(self, item_id: str, kind: str, value: float, ts: str) -> bool:
        """幂等：同一 (item_id, kind) 重复只计一次。True=首次记录。"""
        self.init_schema()
        try:
            self.conn.execute(
                "INSERT INTO feedback (item_id, kind, value, ts) VALUES (?,?,?,?)",
                (str(item_id), str(kind), float(value), str(ts)),
            )
        except sqlite3.IntegrityError:
            return False
        self.conn.commit()
        return True

    def recent_feedback(self, limit: int) -> list[dict]:
        self.init_schema()
        rows = self.conn.execute(
            "SELECT item_id, kind, value, ts FROM feedback ORDER BY ts DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]

    def feedback_for_item(self, item_id: str) -> dict:
        self.init_schema()
        return {
            row[0]: float(row[1])
            for row in self.conn.execute(
                "SELECT kind, value FROM feedback WHERE item_id = ?", (str(item_id),)
            )
        }

    # ---------------------------------------------------------------- meta
    def get_meta(self, key: str) -> str | None:
        self.init_schema()
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.init_schema()
        self.conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?,?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(key), str(value)),
        )
        self.conn.commit()
