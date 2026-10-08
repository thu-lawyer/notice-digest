"""notice-digest —— 清华校内通知聚合与个性化日报引擎。

分层：
  fetch   : pkuknow.cn 只读 JSON API 客户端
  store   : SQLite 持久化（条目 / 详情 / 权重 / 反馈 / 发送记录）
  timeparse: 中文自由文本时间解析（补齐站点不做的规范化）
  score   : 可解释个性化打分 + 在线学习
  enrich  : 详情补全（限速 + 重试）
  feedback: HMAC 签名的本地反馈接收服务
  cli     : 子命令入口；render / mailer 由渲染层提供，仅惰性转发
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
