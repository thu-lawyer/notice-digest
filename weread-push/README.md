# weread-push

基于开源引擎 [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss) 的微信公众号订阅与每日邮件摘要工具链：

- **订阅导入**：132 个公众号通过微信 `searchbiz` 接口批量发现（CDP 真实浏览器通道），按 `fakeid == __biz == mp_id` 写入引擎。
- **增量抓取**：引擎 `message_task` 每 30 分钟同步全部订阅号，新文章自动去重入库（首轮全量：132/132，新增 607 篇）。
- **查询接口**：REST API + 每号 RSS，见 [docs/api.md](docs/api.md)。
- **每日邮件**：`digest/gzhmail.py` 读引擎 SQLite（只读），智谱 GLM 排序出「今日重点」，SMTP 发 HTML 日报，systemd timer 每天 08:15。

## 目录

```
digest/gzhmail.py      # 每日个性化邮件（LLM 排序 + 关键词兜底，凭据全走 env）
import/mp_searchbiz.py # CDP searchbiz 客户端（幂等，>=2.5s/次限速）
feeds/feeds-132.json   # 订阅清单 [{name, biz}] ×132（公开信息）
docs/api.md            # 引擎查询/任务 API 契约（含踩坑注记）
docs/RUNBOOK.md        # 服务器部署与运维手册
```

## 隐私

仓库不含任何凭据、服务器地址、邮箱；`feeds-132.json` 仅为公开公众号名称与其公开 biz 标识。运行时配置一律走 `.env`（不进 git）。

## 部署概要

服务器要求：Python 3.10+（无额外依赖），we-mp-rss 引擎已运行。详见 RUNBOOK。
