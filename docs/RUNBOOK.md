# notice-digest 运行手册（RUNBOOK）

面向：部署与日常运维本工具（每日 07:30 抓取清华/人大校内通知 → 排序 → HTML 邮件 + ICS 投递）。
本文只写**已实测确认**的行为；命令可直接复制执行。

## 1. 数据流与组件

`fetch`（列表分页 + 按 id 去重）→ `enrich`（详情补全 + 中文时间解析）→ `score`（个性化排序 + 在线学习）
→ `render`（HTML + ICS）→ `send`（SMTP 投递）。

反馈回路由 `feedback-serve` 承接：邮件内 👍/👎 链接与点击跳转都打到它，命中后立即更新权重。

## 2. 环境与依赖

- Python 3.12（本机实测 `/opt/anaconda3/bin/python3`）；第三方依赖仅 `requests`（见 requirements.txt），其余为标准库。
- 存储：SQLite（启用 WAL）；时间基准：东八区。
- 所有命令在项目根目录执行（服务器上为 `/opt/notice-digest`）。

## 3. 配置

取值优先级：环境变量 > `.env` > `profile.yaml` > 内置默认值。

| 环境变量 | 作用 |
| --- | --- |
| `ND_TO_ADDR` | 日报收件地址 |
| `ND_FROM_ADDR` | 发件显示地址 |
| `ND_SMTP_HOST` / `ND_SMTP_PORT` | SMTP 服务器与端口（默认 465，SSL） |
| `ND_SMTP_USER` / `ND_SMTP_PASS` | SMTP 账号与口令 |
| `ND_FEEDBACK_BASE` | 反馈服务对外地址。**必须是非回环的实际可达地址**；仍为回环时启动会打印警告，需按第 6 节配置反向代理 |
| `ND_HMAC_SECRET` | 反馈链接签名密钥；缺失时反馈功能不启用 |

`profile.yaml` 中的非敏感默认值：`campus`、`top_n`、`send_at`、`paths.db_path`、`weights_prior`、`keywords_boost`、
`keywords_mute`、`source_boost`、`sections`、`fetch.timeout/retries/pages`。

注意：`.env` 不入版本库；任何口令类取值只放在服务器本地或密钥管理工具里。

## 4. 子命令一览

| 子命令 | 用途 |
| --- | --- |
| `fetch` | 抓取列表页（分页 + 按 id 去重增量） |
| `enrich` | 详情补全（限速 ≥1s/请求） |
| `report` | 输出文本版日报（不依赖渲染层） |
| `render` | 生成 HTML/ICS |
| `send` | 投递邮件（可 `--dry-run`） |
| `stats` | 库统计 |
| `explain` | 解释某条（或前 N 条）打分理由 |
| `learn-selftest` | 在线学习自检（幂等、权重边界、可解释性、学习链路） |
| `feedback-serve` | 启动反馈接收服务（HMAC 签名校验） |
| `units` | 生成 systemd unit/timer 文本（含 `OnFailure=`） |
| `logs` | 查询最近运行事件（含异常分支） |
| `notify-failure` | systemd `OnFailure=` 的落点 |

示例：

```bash
python3 -m notice_digest.cli fetch  --campus thu --pages 8 --db data/notice.db
python3 -m notice_digest.cli enrich --db data/notice.db
python3 -m notice_digest.cli stats  --db data/notice.db
python3 -m notice_digest.cli send   --db data/notice.db --dry-run
python3 -m notice_digest.cli units > /tmp/notice-digest.units
```

## 5. 退出码分级（运维要点）

| 情形 | 退出码 | 是否发失败邮件 |
| --- | --- | --- |
| 网络不可达 / 5xx 重试后仍失败、响应非 JSON、结构漂移、首页 items 为空 | 3 | 是（mailer 发「运行失败」主题） |
| 末页重复（越界页被站点钳制回最后一页）、零新增、达到最大页数、单条详情 404、部分详情失败 | 0 | 否（JSON + stderr + 运行日志记 warn） |

判据：只有「重试也拿不到可用数据」才算致命；可重试/可自愈的偏差只记录，不制造邮件噪音。

## 6. 异常代码与严重级

- **error 级**（退出码 3 + 失败邮件）：`structure-drift`、`page-1-empty`、`parse-failure-spike`、`detail-fetch-all-failed`、fetch/enrich 抛出的异常。
- **warn 级**（仅记录，不影响退出码）：`zero-new`、`max-pages-hit`、`nothing-to-enrich`、`page-repeat`、`detail-404`、`enrich-item-errors`。

## 7. 反馈服务与反向代理

- 默认只监听回环：`python3 -m notice_digest.cli feedback-serve`（127.0.0.1:8791）。
- 健康检查：`curl -sS http://127.0.0.1:8791/nd/health`。
- 对外暴露必须经反向代理，例如 nginx：

```nginx
location /nd/ {
    proxy_pass http://127.0.0.1:8791;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header User-Agent $http_user_agent;
}
```

配好后把 `ND_FEEDBACK_BASE` 设为该对外地址（含域名与路径前缀）。

- **预取防护**：邮件客户端与安全网关会预先抓取邮件里的链接，制造假点击。以下会被判定为预取并忽略：
  GoogleImageProxy / YahooMailProxy / Outlook(Exchange) 预览代理、barracuda、Proofpoint、Mimecast、安全网关，以及 UA 中含 `bot`/`preview`/`prefetch`/`spider` 等。
  **通用 HTTP 客户端库名（`python-urllib`、`requests`、测试默认 UA）不算证据**，一律按真实反馈计入。
  预取请求被丢弃时是**可观测**的：返回 202 且带响应头 `X-ND-Skipped: prefetch`，同时在 stderr 打一行忽略日志（绝不清默吞掉）。
  显式 👍/👎 另有 `(item_id, kind)` 幂等保护，重复点击不会重复学习。

## 8. 定时运行

- systemd：用 `units` 生成 unit/timer（07:30 触发，含 `OnFailure=`，失败落在 `notify-failure`），部署目录 `/opt/notice-digest`、运行用户 `notice-digest`。
- 与同机其他定时任务错峰（本工具固定在 07:30，不要与其他任务的整点任务挤在一起）。
- 运行记录：`python3 -m notice_digest.cli logs --db data/notice.db`。

## 9. 数据源硬约束（照做，不要重新试错）

- `campus` 由 URL 路径前缀决定（`/thu/` 清华、`/ruc/` 人大）。
- `page_size` 参数被忽略，**恒返 30 条/页**。
- **page 超出范围时站点钳制回最后一页** → 因此末页出现整页重复是正常现象：按 all-known 收敛并记 `page-repeat` warn，不属于故障。
- **不存在「page=43 起 `items` 为 null」这回事**（旧口径，已由 t8 实测推翻）：实测 1–43 页各 30 条、第 44 页 9 条、第 45 页起服务端钳制返回与末页相同的一批。**页数会漂移**，因此**不能用固定页数判断收敛**，只能用三条件之一：**整页 id 已知 / 与上页 id 相同 / 触顶**。
- 全部日期区间参数无效 → 增量只能靠分页 + 按 id 去重。
- 站点窗口约 7 天。**日增不是 150–180 条**（旧口径已由实测推翻）：实测**均值约 5 条/日、峰值 112 条**。停机补抓仍需提高 `--pages` 覆盖窗口（站点不认日期区间参数，见上一条）。

## 10. 常见排查

| 症状 | 处理 |
| --- | --- |
| stderr 出现「feedback_base 指向本机回环地址」 | 按第 7 节配反代并设置 `ND_FEEDBACK_BASE` |
| 退出码 3 且收到失败邮件 | 用 `logs` 看 error 级事件定位阶段（fetch / enrich） |
| 入库量明显偏少 | 先看是否已告警 `structure-drift`/`page-1-empty`；再核对站点结构是否变化 |
| 排序看起来不动 | `explain` 查单条理由；权重有上下界与每日衰减，单次反馈不会让结果突变 |
| `learn-selftest` 失败 | 属引擎自检回归（反馈幂等 / 权重不越界 / 可解释 / 学习链路），先修再用 |

## 11. 备份

- 备份 `data/*.db`（WAL 模式下含 `-wal`/`-shm`）与配置。
- 服务运行中请用 `sqlite3 data/notice.db ".backup out.db"`，不要直接 `cp` 正在写入的库。

## 12. 已知残余风险（部署时必须知情）

以下五项是**已知且已被接受**的残余项，来自独立复现（t8）。部署与后续修改都不应以「没听说」为由忽略，也不应在没有反证的情况下改测试断言。

| # | 残余项 | 表现 | 影响面 | 处置建议 |
| --- | --- | --- | --- | --- |
| 1 | **英文月名时间不解析** | 形如 `Oct 15` / `October 15` 的自由文本时间不会被中文时间解析器识别 | 该条落「时间待定」（`undated` / 页脚提示），**不会丢条** | 低危；如需支持，扩展 `notice_digest/` 里的时间解析分支（属代码改动，须走测试） |
| 2 | **反馈令牌没有 `exp`** | HMAC 令牌不含过期时间 | 令牌一旦泄露可长期复用 | 令牌只出现在用户自己的邮件里；如需收紧，加 `exp` 并同步改签名/校验两侧 |
| 3 | **反馈端点自身无速率限制** | `feedback-serve` 内部不做限流 | 公网暴露后可能被刷 | **必须由 nginx 侧限流**（见第 7 节：`location /nd/` 必须带 `limit_req`）；同时服务只监听 127.0.0.1 |
| 4 | **远期日期落 `undated`** | 超出分区窗口的日期不进入「本周/下周」 | 条目仍在，只是分区不精确 | 低危；分区窗口如需扩展，改 `BUCKET_ORDER` 相关逻辑 |
| 5 | **生产不配公网基址就没有反馈按钮** | `ND_FEEDBACK_BASE` 为回环地址时不渲染 👍/👎 | 反馈回路静默失效（**是安全降级，不是 bug**） | 生产 `.env` 必须同时设置 `ND_FEEDBACK_BASE`（公网可达）与 `ND_HMAC_SECRET` |

### 12.1 时间解析完全依赖限速详情端点

- **列表载荷不带时间文本**（`ai_event_time` / `ai_event_summary` 已不在列表里），时间只能从**详情端点**取；
- 详情端点按 **1 req/s** 限速，因此 `enrich` 是整条流水线里最慢的一步（`--limit 120` 约需 2 分钟以上）；
- **enrich 失败不丢条目**：代码里没有任何「按 bucket 丢弃」的路径，失败条目落「时间待定」（页脚体现），仍会出现在邮件里；
- 推论：**不要**为了「看起来更干净」而在 enrich 失败时过滤条目——那会引入丢数据的新缺陷。

### 12.2 断言变更的硬纪律

- **任何断言变更都必须重跑项目目录外的反证树**：在项目目录**之外**复制一份代码，人工注入被该断言保护的缺陷，确认修改后的断言**确实变红并指名违规行**；没有这条反证，断言修改不算证据。
- 反证树的位置约定：`<云盘>/Vesper缓存/临时工作区/`（**不进仓库、不进项目目录**）。
- 若发现「任何合规输出都满足不了原断言」，正确表述是「**该断言禁止了产品必须产出的输出**」，并附反证；不得以「断言太严」为由直接放宽。
- 测量方（测试文件所有者）与实现方分离：实现方只提交主张与复现步骤，**不得直接改他人 inScope 的断言**。

### 12.3 相关修复记录与回归测试

- 修复过程记录：`docs/repair-round-2.md`
- 对应回归测试：`tests/test_repair_round2.py`（随 `python -m unittest discover -s tests` 一起跑）
- 反馈相关行为（签名校验、403 拒绝、健康检查）：`tests/` 下的反馈用例

## 13. 参考代码位置（排查时的入口）

| 关切 | 文件 |
| --- | --- |
| HTTP 抓取与 403/会话处理 | `notice_digest/fetch.py` |
| 中文时间解析、事件抽取 | `notice_digest/` 的时间解析模块 |
| 排序与在线学习 | `notice_digest/score*.py`、`weights` 表 |
| HTML 邮件与 ICS | `notice_digest/render.py` |
| SMTP 投递与台账 | `notice_digest/mailer.py` |
| 幂等闸门与数据访问 | `notice_digest/store.py` |
| 子命令入口 | `notice_digest/cli.py` |
| 反馈服务 | `notice_digest/feedback.py` |
