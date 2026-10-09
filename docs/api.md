# we-mp-rss 引擎 API 契约（v1.5.3 实测）

引擎仅监听 `127.0.0.1:8792`，以下均为本机路径。鉴权：`Authorization: Bearer <token>`。
SPA 全局捕获 GET：`/openapi.json` 返回 HTML，调试用 venv python 导入 `apis/` 内省。

## 认证

```
POST /api/v1/wx/auth/login
Content-Type: application/x-www-form-urlencoded

username=...&password=...
→ {"code":0,"data":{"access_token":"..."}}   # OAuth2PasswordRequestForm，不接受 JSON
```

## 公众号（mps）

```
GET  /api/v1/wx/mps?offset=0&limit=100      # data.list + data.page{limit,offset,total}
POST /api/v1/wx/mps   {"mp_name":"名称","mp_id":"<biz/fakeid>"}   # 按 faker_id 幂等
```

- 列表项主键 `id` = `MP_WXS_<base64(biz)>`；`mp_id`（即 searchbiz 的 `fakeid`、微信链接里的 `__biz`）不出现在列表投影里，需自行换算：

```
s = biz.replace('-','+').replace('_','/')
s += '=' * (-len(s) % 4)
feed_id = 'MP_WXS_' + base64.b64decode(s).decode()
```

## 文章（articles）——「查看全部消息」

```
GET /api/v1/wx/articles?offset=0&limit=20&mp_id=MP_WXS_xxx&search=关键词&has_content=true
```

| 参数 | 说明 |
|---|---|
| offset / limit | 分页，limit ≤ 100，按 `publish_time` 降序 |
| mp_id | 单号过滤（`MP_WXS_*`） |
| search | 标题关键词 |
| status | 逗号分隔；默认已排除 deleted |
| has_content | true=已有正文 / false=无 / 不传=全部 |
| only_favorite | 收藏过滤 |

- 详情：`GET /api/v1/wx/articles/{article_id}`（含 `content`/`content_html` 全文）。
- 阅读态/收藏：`PUT /api/v1/wx/articles/{article_id}/read`、`/favorite`。
- 单篇刷新：`POST /api/v1/wx/articles/{article_id}/refresh`；任务态 `GET /articles/refresh/tasks/{task_id}`。
- 增量保证：同步任务按 URL 去重入库；底层 SQLite `data/db.db` 表 `articles`（`publish_time` INTEGER 秒，`created_at` DATETIME）。

## RSS（无鉴权）

```
GET /rss/{feed_id}?limit=20&offset=0&ext=xml     # feed_id = MP_WXS_*
```

## 消息任务（message_tasks）

```
GET  /api/v1/wx/message_tasks
GET  /api/v1/wx/message_tasks/{id}
GET  /api/v1/wx/message_tasks/{id}/run       # 手动执行一次（同步阻塞，132 号约数分钟）
PUT  /api/v1/wx/message_tasks/{id}
PUT  /api/v1/wx/message_tasks/job/fresh      # 改完必须重载调度
```

**PUT 契约坑**：不是部分更新。必填 `message_template`、`web_hook_url`，缺了直接 422；
`mps_id` 是 **JSON 字符串**（不是数组对象）：`"[{\"id\":\"MP_WXS_xxx\"}, ...]"`。
改订阅号集合 = GET 取旧值 → 按 `it['id']` 去重合并 → PUT 全量 → `PUT job/fresh`。

## 审计记录

- 2026-10-10 首轮全量：run 返回 `已触发 132 个订阅号同步，本次新增 607 篇文章`；articles 共 4341 行、distinct mp_id = 132。
