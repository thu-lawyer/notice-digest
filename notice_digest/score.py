"""可解释个性化打分 + 在线学习。

打分是**可解释**的：每个特征命中产出一条「特征名, 贡献分」，总分即各贡献之和。
特征族：
    cat:<分类>        分类 one-hot（文体/讲座/比赛等）
    kw:<关键词>       标题/摘要里的关键词命中（讲座、竞赛、法学、AI …）
    src:<来源>        来源偏置
    time:<窗口>       事件时间紧迫度（今天/明天/本周/下周）
    dl:<窗口>         报名截止紧迫度
    mute:<关键词>     负向词（招聘、广告、二手 …）

在线学习：有界加性梯度更新（logistic 风格的 online SGD）
    w_f += lr * (reward - sigmoid(score)) * v_f
并对所有特征做硬上下界裁剪，防止被单条反馈带飞。
``decay`` 每日把权重乘一个略小于 1 的因子，防锁死。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from . import config as config_mod
from .timeparse import ParsedTime

#: 权重硬边界（在线学习绝不越界）
W_MIN = -5.0
W_MAX = 5.0

#: 默认学习率
DEFAULT_LR = 0.12

#: 沉默弱负样本（展示了但没点）：用弱正奖励近似「没被排除」
SILENT_REWARD = 0.2

#: 时间紧迫度先验
TIME_PRIORS = {
    "time:today": 1.20,
    "time:tomorrow": 0.90,
    "time:this_week": 0.50,
    "time:next_week": 0.20,
    "dl:today": 1.50,
    "dl:tomorrow": 1.10,
    "dl:this_week": 0.60,
    "dl:next_week": 0.20,
}

#: 需要参与关键词匹配的正文/摘要长度上限
TEXT_SCAN_LIMIT = 2000

_BUCKET_FEATURE = {
    "today": "today",
    "tomorrow": "tomorrow",
    "this_week": "this_week",
    "next_week": "next_week",
}


@dataclass
class Scored:
    item: dict
    score: float = 0.0
    #: [(特征名, 贡献分), ...]
    reasons: list[tuple[str, float]] = field(default_factory=list)
    parsed: ParsedTime | None = None

    @property
    def item_id(self) -> str:
        return str(self.item.get("id") or "")

    @property
    def title(self) -> str:
        return str(self.item.get("title") or "")

    @property
    def reason_strings(self) -> list[str]:
        return [f"{name}, {value:+.2f}" for name, value in self.reasons]

    def as_dict(self) -> dict:
        return {
            "id": self.item_id,
            "title": self.title,
            "score": round(self.score, 4),
            "reasons": [{"feature": n, "contribution": round(v, 4)} for n, v in self.reasons],
            "bucket": self.parsed.bucket if self.parsed else None,
        }


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def clip_weight(value: float) -> float:
    return max(W_MIN, min(W_MAX, float(value)))


def _match_text(item: dict) -> str:
    """拼出用于关键词匹配的文本（标题 + 摘要 + 正文片段）。"""
    parts = [str(item.get("title") or "")]
    english = item.get("english") or {}
    if isinstance(english, dict):
        for key in ("title", "source_name"):
            value = english.get(key)
            if value:
                parts.append(str(value))
    detail = item.get("detail") or {}
    if isinstance(detail, dict):
        for key in ("ai_summary", "content_markdown", "content", "ai_event_location", "organizer"):
            value = detail.get(key)
            if value:
                parts.append(str(value))
    text = "\n".join(parts)
    return text[:TEXT_SCAN_LIMIT]


def _token_hit(text: str, token: str) -> bool:
    if not token:
        return False
    if token.isascii():
        return token.lower() in text.lower()
    return token in text


def features_of(
    item: dict,
    cfg: config_mod.Config,
    now: datetime,
    parsed: ParsedTime | None = None,
) -> dict[str, float]:
    """抽取条目特征（one-hot / 命中计数）。

    返回 dict[特征名, 特征值]，特征值为 1.0（存在性）为主。
    """
    feats: dict[str, float] = {}

    category = (item.get("category") or "").strip()
    if category:
        feats[f"cat:{category}"] = 1.0

    source = (item.get("source_name") or "").strip()
    if source:
        feats[f"src:{source}"] = 1.0

    text = _match_text(item)
    # keywords_boost 的键本身即特征名（"cat:讲座活动" / "kw:讲座"），匹配时去掉前缀。
    boost_keys = cfg.keywords_boost or config_mod.default_keywords_boost()
    for feature in boost_keys:
        name = str(feature)
        token = name.split(":", 1)[1] if ":" in name else name
        if _token_hit(text, token):
            feats[name] = 1.0

    for token in cfg.keywords_mute or config_mod.default_keywords_mute():
        if _token_hit(text, str(token)):
            feats[f"mute:{token}"] = 1.0

    if parsed is not None:
        bucket = _BUCKET_FEATURE.get(parsed.bucket)
        if bucket:
            feats[f"time:{bucket}"] = 1.0
        if parsed.deadline is not None:
            dl = _deadline_bucket(parsed.deadline, now)
            if dl:
                feats[f"dl:{dl}"] = 1.0

    return feats


def _deadline_bucket(deadline: datetime, now: datetime) -> str | None:
    from .timeparse import _far_bucket  # 复用同一套窗口定义

    bucket = _far_bucket(deadline, now)
    return _BUCKET_FEATURE.get(bucket)


def merged_weights(
    cfg: config_mod.Config,
    stored: dict | None = None,
) -> dict[str, float]:
    """合并权重：默认先验 ← 配置先验 ← 库中已学权重（后者覆盖前者）。"""
    base: dict[str, float] = dict(TIME_PRIORS)
    base.update(config_mod.default_weights_prior())
    base.update(cfg.weights_prior or {})
    # 来源先验的键不带前缀（"清华法学院通知"），特征名统一为 "src:..."
    for name, value in (cfg.source_boost or config_mod.default_source_boost()).items():
        key = str(name)
        if not key.startswith("src:"):
            key = f"src:{key}"
        try:
            base.setdefault(key, float(value))
        except (TypeError, ValueError):
            continue
    for token in cfg.keywords_mute or config_mod.default_keywords_mute():
        base.setdefault(f"mute:{token}", -1.2)
    for key, value in (stored or {}).items():
        try:
            base[str(key)] = clip_weight(float(value))
        except (TypeError, ValueError):
            continue
    return base


def score_items(
    items: Iterable[dict],
    weights: dict | None,
    cfg: config_mod.Config,
    now: datetime,
    parsed_map: dict | None = None,
) -> list[Scored]:
    """给条目打分并给出理由，按分数降序返回。

    ``parsed_map``: {item_id: ParsedTime}，可选；提供时会加入时间紧迫度特征。
    """
    w = merged_weights(cfg, weights)
    parsed_map = parsed_map or {}
    out: list[Scored] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        parsed = parsed_map.get(item_id)
        feats = features_of(item, cfg, now, parsed)
        reasons: list[tuple[str, float]] = []
        total = 0.0
        for name, value in feats.items():
            weight = w.get(name, 0.0)
            contribution = weight * value
            if contribution == 0.0:
                continue
            reasons.append((name, contribution))
            total += contribution
        reasons.sort(key=lambda pair: abs(pair[1]), reverse=True)
        out.append(Scored(item=item, score=total, reasons=reasons, parsed=parsed))
    out.sort(key=lambda s: (-s.score, s.item_id))
    return out


def learn(weights: dict, feats: dict, reward, lr: float = DEFAULT_LR) -> dict:
    """单样本在线更新（t7-F5）：按 |sigmoid'| 归一化做饱和补偿，并限制单步幅度。

    旧实现直接用 (reward - p) 乘 lr：p 接近 0/1 时梯度趋于 0（对 6 分的条目连点
    200 次 👍 也几乎不动），而 p≈0.5 时一步就能把权重推很远 —— 饱和端失效、
    中间端过冲。现在步长 = 目标边际误差 / |sigmoid'(score)|，再夹到 ±MAX_STEP。
    """
    if not feats:
        return dict(weights)
    out = dict(weights)
    score = sum(float(out.get(f, 0.0)) * float(v) for f, v in feats.items())
    step = _bounded_step(score, reward, lr)
    if step == 0.0:
        return out
    for f, v in feats.items():
        out[f] = _clip(float(out.get(f, 0.0)) + step * float(v))
    return out



def apply_feedback(weights: dict, item: dict, cfg, now, reward, parsed=None, lr: float = DEFAULT_LR) -> dict:
    """把一次反馈落到权重上（同样走有界边际步长，避免一次点赞把分数打飞）。"""
    feats = features_of(item, cfg, now, parsed)
    if not feats:
        return dict(weights)
    score = sum(float(weights.get(f, 0.0)) * float(v) for f, v in feats.items())
    step = _bounded_step(score, reward, lr)
    out = dict(weights)
    if step == 0.0:
        return out
    for f, v in feats.items():
        out[f] = _clip(float(out.get(f, 0.0)) + step * float(v))
    return out


def apply_feedback_deltas(deltas: dict, merged: dict, item: dict, cfg, now, reward, parsed=None, lr: float = DEFAULT_LR) -> dict:
    """只更新「训练出来的增量」表（t7-F4）。

    分数用合并后的权重（先验+增量）算，但落盘只写 deltas —— 于是：
    · weights 表键集只包含真正被反馈过的特征，不会被 49 条配置先验灌满；
    · 每日衰减只作用于学习出来的增量，先验永远不会被侵蚀；
    · 单次点击对总分的影响有上界（MAX_STEP × 特征数）。
    """
    feats = features_of(item, cfg, now, parsed)
    if not feats:
        return dict(deltas)
    score = sum(float(merged.get(f, 0.0)) * float(v) for f, v in feats.items())
    step = _bounded_step(score, reward, lr)
    out = dict(deltas)
    if step == 0.0:
        return out
    for f, v in feats.items():
        out[f] = _clip(float(out.get(f, 0.0)) + step * float(v))
    return out



def decay(weights: dict, factor: float = 0.995) -> dict:
    """每日衰减：所有特征权重乘以 factor，再做硬裁剪。"""
    f = float(factor)
    return {str(k): clip_weight(float(v) * f) for k, v in (weights or {}).items()}


def explain(scored: Scored | list[Scored] | dict, top: int | None = None) -> str:
    """把打分理由渲染成可读文本，每条格式固定为「特征名, 贡献分」。"""
    if isinstance(scored, dict):
        scored = Scored(item=scored)
    if isinstance(scored, list):
        lines = []
        for s in scored[: top or 10]:
            lines.append(explain(s))
        return "\n\n".join(lines)

    lines = [f"{scored.title}  →  总分 {scored.score:+.2f}"]
    reasons = scored.reasons if top is None else scored.reasons[:top]
    for name, value in reasons:
        lines.append(f"  {name}, {value:+.2f}")
    if not reasons:
        lines.append("  （无命中特征）")
    if scored.parsed is not None and scored.parsed.evidence:
        lines.append(f"  时间证据: {scored.parsed.evidence}")
    return "\n".join(lines)


# ---------------------------------------------------------------- t7-F5 有界步长
REWARD_UP = 0.8
REWARD_DOWN = 0.2
REWARD_CLICK = 0.5
MARGIN_UP = 0.995
MAX_STEP = 0.05
SLOPE_FLOOR = 0.02


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _slope(x: float) -> float:
    p = _sigmoid(x)
    return max(p * (1.0 - p), SLOPE_FLOOR)


def _clip(w: float) -> float:
    return max(W_MIN, min(W_MAX, w))


def _bounded_step(score: float, reward, lr: float = DEFAULT_LR) -> float:
    """Hinge 目标 + 饱和补偿：p→0/1 时步长不塌陷，单步不超过 MAX_STEP。

    reward >= 0.5 视为正向（👍/点击），否则负向（👎）。对称目标 0.995/0.005
    与 REWARD_UP=0.8 / REWARD_DOWN=0.2 的基线一致，同时保证 p 处在任何位置
    都有非零位移（不会出现「点了一次永远不变」的死区）。
    """
    p = _sigmoid(score)
    up = float(reward) >= 0.5
    target = MARGIN_UP if up else (1.0 - MARGIN_UP)
    err = (target - p) if up else (p - target)
    if err <= 0.0:
        return 0.0
    step = lr * err / _slope(score)
    if step > MAX_STEP:
        step = MAX_STEP
    return step if up else -step
