"""配置加载。

零配置可跑：profile.yaml 与 .env 都可缺失，缺失时使用内置默认值。
任何凭据都不写进代码：smtp_pass / hmac_secret 一律从环境变量或 .env 读取。

仅依赖标准库（自带一个覆盖本文件所需子集的 YAML 解析器），
以便在没有 PyYAML 的服务器上也能直接运行。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PROFILE_RELPATH = Path("..") / "profile.yaml"
DEFAULT_ENV_RELPATH = Path("..") / ".env"

DEFAULT_SECTIONS = [
    "今天",
    "明天",
    "本周",
    "讲座活动",
    "文体活动",
    "比赛竞赛",
    "法学",
    "AI 相关",
    "其他",
]

#: 时间不可解析时的兜底分桶名
BUCKET_ORDER = ["today", "tomorrow", "this_week", "next_week", "undated", "past"]


@dataclass
class Config:
    campus: str = "thu"
    db_path: Path = Path("data/notice.db")
    top_n: int = 20
    send_at: str = "07:30"
    to_addr: str = ""
    from_addr: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    feedback_base: str = "http://127.0.0.1:8791"
    hmac_secret: str = ""
    weights_prior: dict = field(default_factory=dict)
    keywords_boost: dict = field(default_factory=dict)
    keywords_mute: list = field(default_factory=list)
    source_boost: dict = field(default_factory=dict)
    sections: list = field(default_factory=lambda: list(DEFAULT_SECTIONS))

    # --- 非契约字段（可选，缺省即可） -------------------------------
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_ssl: bool = True
    timeout: int = 20
    retries: int = 3
    pages: int = 8

    # --- 公众号文章源（gzh_source；全部缺省=该源关闭，行为与旧版完全一致） ---
    gzh_db: str = ""             # we-mp-rss 只读 SQLite（ND_GZH_DB）
    gzh_state: str = ""          # 已发送台账（ND_GZH_STATE；缺省用 <gzh_db 同目录>/state-gzh.json）
    gzh_top: int = 10            # LLM 精选篇数（ND_GZH_TOP）
    gzh_lookback: int = 26       # 增量窗口小时数（ND_GZH_LOOKBACK）
    gzh_zhipu_api_key: str = ""  # 智谱 API key（ND_ZHIPU_API_KEY）
    gzh_llm_model: str = ""      # 智谱模型名（ND_LLM_MODEL）

    def clamp_top_n(self, value: int | None = None) -> int:
        n = self.top_n if value is None else value
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 20
        return max(1, min(n, 200))


def default_weights_prior() -> dict:
    """内置先验：文体、讲座、比赛、法学、AI 加权。"""
    return {
        "cat:讲座活动": 1.6,
        "cat:文体活动": 1.2,
        "cat:社团公益": 0.5,
        "cat:学术科研": 0.8,
        "cat:校园动态": 0.15,
        "cat:学业教务": 0.3,
        "kw:讲座": 1.0,
        "kw:论坛": 0.7,
        "kw:报告": 0.6,
        "kw:学术": 0.5,
        "kw:比赛": 1.1,
        "kw:竞赛": 1.1,
        "kw:大赛": 1.0,
        "kw:报名": 0.35,
        "kw:招募": 0.3,
        "kw:法学": 1.3,
        "kw:法律": 1.2,
        "kw:法治": 1.1,
        "kw:法学院": 1.4,
        "kw:宪法": 0.9,
        "kw:民法": 0.9,
        "kw:刑法": 0.9,
        "kw:行政法": 0.9,
        "kw:AI": 1.2,
        "kw:人工智能": 1.2,
        "kw:机器学习": 1.0,
        "kw:大模型": 1.0,
        "kw:算法": 0.7,
        "kw:数据": 0.5,
        "kw:计算法学": 1.3,
    }


def default_keywords_boost() -> dict:
    return dict(default_weights_prior())


def default_keywords_mute() -> list:
    return [
        "招聘",
        "宣讲会",
        "夏令营",
        "冬令营",
        "推销",
        "广告",
        "团购",
        "二手",
    ]


def default_source_boost() -> dict:
    return {
        "清华法学院通知": 1.4,
        "清华大学图书馆培训讲座": 1.0,
        "清华大学图书馆通知": 0.6,
    }


# --------------------------------------------------------------------------
# 极简 YAML 子集解析（映射 / 列表 / 标量），足够读 profile.yaml
# --------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    out = []
    quote = None
    for idx, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (idx == 0 or line[idx - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(text: str) -> Any:
    text = text.strip()
    if text == "":
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    low = text.lower()
    if low in ("null", "none", "~"):
        return None
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?\d*\.\d+([eE][+-]?\d+)?", text):
        return float(text)
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in inner.split(",")]
    return text


def simple_yaml_load(text: str) -> dict:
    """把 YAML 子集解析成 dict；无法解析时抛 ValueError。"""
    root: dict = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if body.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"列表项出现在非列表上下文: {body!r}")
            parent.append(_scalar(body[2:]))
            continue

        m = re.match(r"^([^:]+):\s*(.*)$", body)
        if not m:
            raise ValueError(f"无法解析的行: {body!r}")
        key = m.group(1).strip().strip("\"'")
        rest = m.group(2)

        if rest.strip() == "":
            # 看下一个非空行的缩进决定是 dict 还是 list
            nxt = _next_body(text, raw)
            container: Any = [] if (nxt and nxt.lstrip().startswith("- ")) else {}
            parent[key] = container
            stack.append((indent, container))
        else:
            parent[key] = _scalar(rest)

    return root


def _next_body(text: str, after: str) -> str | None:
    lines = text.splitlines()
    try:
        idx = lines.index(after)
    except ValueError:
        return None
    for line in lines[idx + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = _strip_comment(line)
        if stripped.strip():
            return stripped
    return None


def load_env_file(path: Path) -> dict:
    """读 .env（KEY=VALUE），返回 dict；文件不存在返回空。"""
    env: dict = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return env
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            env[key] = value
    return env


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_dict(value: Any, default: dict) -> dict:
    return dict(value) if isinstance(value, dict) else dict(default)


def _as_list(value: Any, default: list) -> list:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return list(default)


def load_config(
    profile_path: Path | None = None,
    env_path: Path | None = None,
) -> Config:
    """加载配置。两个路径都可缺失，缺失即用内置默认值。"""
    project_root = Path(__file__).resolve().parent.parent
    profile_path = Path(profile_path) if profile_path is not None else project_root / DEFAULT_PROFILE_RELPATH
    env_path = Path(env_path) if env_path is not None else project_root / DEFAULT_ENV_RELPATH

    profile: dict = {}
    if profile_path.exists():
        try:
            profile = simple_yaml_load(profile_path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeDecodeError, ValueError):
            profile = {}

    env_file = load_env_file(env_path)

    def opt(name: str, default: str = "") -> str:
        return os.environ.get(name) or env_file.get(name) or default

    smtp = _as_dict(profile.get("smtp"), {})
    feedback = _as_dict(profile.get("feedback"), {})
    paths = _as_dict(profile.get("paths"), {})
    fetch_cfg = _as_dict(profile.get("fetch"), {})

    db_default = project_root / "data" / "notice.db"
    db_path = Path(_as_str(paths.get("db_path"), str(db_default)))

    cfg = Config(
        campus=_as_str(profile.get("campus"), "thu"),
        db_path=db_path,
        top_n=_as_int(profile.get("top_n"), 20),
        send_at=_as_str(profile.get("send_at"), "07:30"),
        to_addr=opt("ND_TO_ADDR", _as_str(profile.get("to_addr"), "")),
        from_addr=opt("ND_FROM_ADDR", _as_str(profile.get("from_addr"), "")),
        smtp_host=opt("ND_SMTP_HOST", _as_str(smtp.get("host"), "")),
        smtp_port=_as_int(opt("ND_SMTP_PORT", "") or smtp.get("port"), 465),
        feedback_base=opt(
            "ND_FEEDBACK_BASE",
            _as_str(feedback.get("base"), "http://127.0.0.1:8791"),
        ),
        hmac_secret=opt("ND_HMAC_SECRET", ""),
        weights_prior=_as_dict(profile.get("weights_prior"), default_weights_prior()),
        keywords_boost=_as_dict(profile.get("keywords_boost"), default_keywords_boost()),
        keywords_mute=_as_list(profile.get("keywords_mute"), default_keywords_mute()),
        source_boost=_as_dict(profile.get("source_boost"), default_source_boost()),
        sections=_as_list(profile.get("sections"), DEFAULT_SECTIONS),
        gzh_db=opt("ND_GZH_DB", ""),
        gzh_state=opt("ND_GZH_STATE", ""),
        gzh_top=_as_int(opt("ND_GZH_TOP", ""), 10),
        gzh_lookback=_as_int(opt("ND_GZH_LOOKBACK", ""), 26),
        gzh_zhipu_api_key=opt("ND_ZHIPU_API_KEY", ""),
        gzh_llm_model=opt("ND_LLM_MODEL", ""),
        smtp_user=opt("ND_SMTP_USER", _as_str(smtp.get("user"), "")),
        smtp_pass=opt("ND_SMTP_PASS", ""),
        smtp_ssl=bool(smtp.get("ssl", True)),
        timeout=_as_int(fetch_cfg.get("timeout"), 20),
        retries=_as_int(fetch_cfg.get("retries"), 3),
        pages=_as_int(fetch_cfg.get("pages"), 8),
    )
    return cfg
