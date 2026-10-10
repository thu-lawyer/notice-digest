# RUNBOOK · 服务器部署与运维

服务器：Ubuntu 24.04，2 核，无 Docker。引擎目录 `/opt/weread-push/we-mp-rss`（FastAPI，127.0.0.1:8792，systemd `weread-engine.service`）。

## 1. 订阅号批量导入

1. 用 CDP 真实浏览器（已登录 mp.weixin.qq.com）跑 `import/mp_searchbiz.py` 逐个查询 `searchbiz`（**≥2.5s/次**，幂等可重跑），得 `{fakeid, nickname}` 清单。
2. `POST /api/v1/wx/mps  {"mp_name": nickname, "mp_id": fakeid}` 逐个导入（按 faker_id 幂等）。
3. 合并进 message_task：GET 任务 → 旧 `mps_id` 与新 feed 集合按 `it['id']` 去重 → PUT 全量（**必带 message_template / web_hook_url**，mps_id 为 JSON 字符串）→ `PUT /job/fresh`。
4. 校验：GET /mps 总数、task 的 mps_id 长度、feeds 覆盖数三者一致。

## 2. 同步任务

- 任务 `*/30 * * * *` 每 30 分钟全量同步一次；手动触发 `GET /api/v1/wx/message_tasks/{id}/run`（阻塞数分钟）。
- 结果存 `data/db.db`（WAL）；查看：`venv/bin/python` + sqlite3（服务器无 sqlite3 CLI）。

## 3. gzhmail 每日邮件（08:15）

```
mkdir -p /opt/gzhmail && cp gzhmail.py /opt/gzhmail/
# env：从现有 SMTP/LLM 凭据文件抽取同名键，勿手抄明文
grep -E '^(SMTP_HOST|SMTP_PORT|SMTP_USER|SMTP_PASS|MAIL_TO|MAIL_FROM_NAME|ZHIPU_API_KEY|LLM_MODEL)=' /opt/aidigest/aidigest.env > /opt/gzhmail/gzhmail.env
chmod 600 /opt/gzhmail/gzhmail.env
# 试跑（不发信）
python3 /opt/gzhmail/gzhmail.py --dry-run
```

systemd：

```ini
# /etc/systemd/system/gzhmail.service
[Unit]
Description=gzhmail daily wechat-mp digest
[Service]
Type=oneshot
WorkingDirectory=/opt/gzhmail
ExecStart=/usr/bin/python3 /opt/gzhmail/gzhmail.py

# /etc/systemd/system/gzhmail.timer
[Unit]
Description=gzhmail daily 08:15
[Timer]
OnCalendar=*-*-* 08:15:00
Persistent=true
[Install]
WantedBy=timers.target
```

`systemctl daemon-reload && systemctl enable --now gzhmail.timer`。

行为：窗口默认 26h（`LOOKBACK_HOURS`），无新文章不发信；已推送按 URL md5 记 `state.json`，重跑不重发。LLM 排序失败自动回退关键词法，不影响发信。

## 4. 已知边界

- `sqlite3` CLI 未安装，一律用 `venv/bin/python`。
- `/openapi.json` 被 SPA 捕获返回 HTML；内省用 venv python import。
- 微信侧对频控敏感：任何批量查询保持 ≥2.5s 间隔。
- 手动 run 与 30 分钟 cron 可能叠跑：引擎按 URL 去重，重复无害。
