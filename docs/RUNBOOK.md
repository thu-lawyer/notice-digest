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
| **详情端点被限流（HTTP 429）** | 0 | 否（记 `detail-rate-limited` warn；本批在首次命中处收批，未补全条目留在 pending 待下一轮） |
| 末页重复（越界页被站点钳制回最后一页）、零新增、达到最大页数、单条详情 404、部分详情失败 | 0 | 否（JSON + stderr + 运行日志记 warn） |

判据：只有「重试也拿不到可用数据」才算致命；可重试/可自愈的偏差只记录，不制造邮件噪音。

判据收窄（t27，2026-10-09）：`detail-fetch-all-failed` 只在「一条都没补全成功」**且**失败不能被限流/404 解释时才致命，判别式 `fetched == 0 and (failed - rate_limited - not_found) > 0`。**全批 429 走 warn + 退出码 0、不发失败邮件**（配额不是结构性缺陷）；响应非 JSON、全 5xx、单条非 `FetchError` 异常等结构性失败仍 exit 3 + 失败邮件。

## 6. 异常代码与严重级

- **error 级**（退出码 3 + 失败邮件）：`structure-drift`、`page-1-empty`、`parse-failure-spike`、`detail-fetch-all-failed`（**仅当 `fetched == 0 and failed - rate_limited - not_found > 0`**；整批因 429 失败不算）、fetch/enrich 抛出的异常。
- **warn 级**（仅记录，不影响退出码）：`zero-new`、`max-pages-hit`、`nothing-to-enrich`、`page-repeat`、`detail-404`、**`detail-rate-limited`**、`enrich-item-errors`。

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
- 服务运行中请用 SQLite 的 `.backup` API，**不要直接 `cp` 正在写入的库**（WAL 下会拿到不一致快照）。
- **生产服务器上没有 `sqlite3` CLI**（t27 实测：`sqlite3: command not found`，退出码 127），用部署 venv 的 Python 执行备份：
  ```
  /opt/notice-digest/.venv/bin/python -c 'import sqlite3,sys; s=sqlite3.connect("/opt/notice-digest/data/notice.db"); d=sqlite3.connect(sys.argv[1]); s.backup(d)' /opt/notice-digest/backup/notice.db
  ```
  本机排查同理，把两个路径换成 `data/notice.db` 与目标文件即可。

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
- 凭据谓词净回归 3816/74880 行、PRE 基线 `ab4d032a3ad8e010…`、详见 `tests/verify_notes.md`。

### 12.3 相关修复记录与回归测试

- 修复过程记录：`docs/repair-round-2.md`
- 对应回归测试：`tests/test_repair_round2.py`（随 `python -m unittest discover -s tests` 一起跑）
- 反馈相关行为（签名校验、403 拒绝、健康检查）：`tests/` 下的反馈用例

### 12.4 投递台账的表结构（按实际 DDL，勿凭记忆写）

- **`sends` 表没有指纹列**：`CREATE TABLE IF NOT EXISTS sends (date TEXT PRIMARY KEY, subject TEXT, n_items INTEGER, sent_at TEXT NOT NULL);` —— 它只回答「某天发过没有」。
- **投递指纹在 `send_attempts` 表**，且是复合主键的一半：`CREATE TABLE IF NOT EXISTS send_attempts (date TEXT NOT NULL, fingerprint TEXT NOT NULL, subject TEXT, n_items INTEGER, status TEXT NOT NULL, started_at TEXT NOT NULL, sent_at TEXT, PRIMARY KEY (date, fingerprint));`，另有索引 `idx_send_attempts_date`（`date DESC, started_at DESC`）。
- 因此「某天某指纹是否已投递」只能查 `send_attempts`（`SELECT status FROM send_attempts WHERE date=? AND fingerprint=?`）；在 `sends` 里找指纹列会报 `no such column`。
- 查库用 Python（服务器无 `sqlite3` CLI，见第 11 节）：
  ```
  /opt/notice-digest/.venv/bin/python -c 'import sqlite3; c=sqlite3.connect("/opt/notice-digest/data/notice.db"); print(c.execute("select date,substr(fingerprint,1,8),status,sent_at from send_attempts order by date desc limit 10").fetchall())'
  ```

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

## 14. 反馈端点 `/nd/f` 的实际契约（逐字实测，以此为准）

任何写「未签名 → 403」的单一状态码表述都与实现不符：**参数校验先于签名校验**。下表按输入类别分列（2026-10-09 直连 `http://127.0.0.1:8791` 实测；对应实现 `notice_digest/feedback.py` L316–L320）：

| 输入类别 | 实测返回 | 说明 |
| --- | --- | --- |
| `k` 缺失 / 非 `up`/`down`（空串、`read`、`click` …） | **400 `bad kind`** | 参数校验阶段即拒绝，**不进入签名校验** |
| `k` ∈ {`up`,`down`}，签名缺失（无 `t` 参数） | **403 `bad signature`** | |
| `k` ∈ {`up`,`down`}，签名错误（`t=` 空串或伪造值） | **403 `bad signature`** | 「`t` 为空串」与「无 `t`」同码 |
| `/nd/c` 未签名 | **403 `bad signature`** | |

- **合法 `k` 只有 `up` 与 `down`**（L317）。`record_and_learn` 内部另接受 `click`（`_reward_for_kind` L126–L127），但 **HTTP 层 `/nd/f` 不接受 `click`**，传入即 400。
- **安全性质成立**：上述全部 5 组畸形/伪造请求执行完毕后，`select count(*) from feedback` 仍为 **0** —— 任何未签名或签名错误的请求都不产生任何写入。因此这是**契约措辞缺陷，不是代码缺陷**；不要为了「把状态码对上」去改 `feedback.py` 的校验顺序（无收益，且会让另一侧的性质反过来依赖顺序）。
- 写验收标准时按「参数畸形 / 签名缺失 / 签名错误」三类分列，不要写单一状态码。

## 15. 反馈入口的公网基址：三个候选（未决，本轮明确不做）

前置事实：`/opt/notice-digest/.env` 的 `ND_FEEDBACK_BASE` 仍为回环地址，故**当前邮件里不渲染 👍/👎 按钮** —— 这是第 12 节第 5 项的安全降级，**不是故障**。要让反馈回路可用，须先决定公网基址：

| 候选 | 做法 | 前置条件 | 代价 / 风险 |
| --- | --- | --- | --- |
| A 独立子域 | 新子域反代到 `127.0.0.1:8791` | 域名解析 + 证书（certbot） | 最干净；新增一张证书与一条站点配置 |
| B 复用现有站点路径 | 在现有 nginx 站点内加 `location /nd/` | 沿用该站点证书 | 无新证书；与现有站点的路径空间耦合 |
| C IP 直连 | `http://<IP>:<端口>` | 无需域名 | **不推荐**：明文 HTTP，反馈令牌会暴露在链路上 |

约束（见第 7 节与第 12 节第 3 项）：无论选哪个，`location /nd/` **必须带 `limit_req`**（`limit_req_zone` 只能写在 http 上下文、`location` 只能写在 server 上下文，故必须拆成两个文件），且服务本身只监听 127.0.0.1。改基址属**基础设施变更，需用户确认**，本轮不做。

## 16. 从本机推送到 GitHub 的稳定性配方（环境特定经验配方，不是承诺）

本机 `github.com` 被 DNS 污染、直连不通。以下配方以「一次成功」为准，换机器/换网络不保证成立：

- **可用配方（2026-10-09 首次尝试即成功，rc=0，`85bb15a..ac8c16e main -> main`）**：
  ```
  git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 \
      -c http.version=HTTP/1.1 push origin main
  ```
  `http.version=HTTP/1.1` 是关键：默认 HTTP/2 会得到 `Empty reply from server`。
- **已失败配方（勿重复尝试）**：不加代理的 `git push origin main` 连续 3 次 rc=128 —— 先 `Empty reply from server`，随后两次 `Failed to connect to github.com port 443 after 75018/75003 ms`。
- 诊断口径：`api.github.com` 可达（200）**不代表** `github.com:443` 可达（两者解析结果不同）；`ssh.github.com:443` 能建 TCP 连接但 `Permission denied (publickey)`。
- **不要用 `gh` CLI**：其钥匙串内 token 已失效。
- **推送后必须核验「远端文件与本地逐字节一致」，而不是只看 push 成功**：
  ```
  git show origin/main:notice_digest/fetch.py | shasum -a 256   # 与本地 shasum -a 256 notice_digest/fetch.py 比对
  ```
- 推送前按约定做脱敏扫描（服务器 IP / 邮箱 / 姓名 / 授权码）。命中若来自**文档在描述自身扫描模式**（自指，如测试笔记里的模式字面量），应逐条说明来源，**不要**为了让扫描归零而改写文档。

## 17. pkuknow 详情端点限流（HTTP 429）的实测曲线与处置（t27）

**结论（先给数据，再给判断）**：本节的曲线由**两轮同出口实测**补成 —— ①在**自限速约 1.05 s/请求**下连续发 **550 次**详情请求，**全部 200，0 个 429**；②在**零节流突发**下，**第 35 个连续详情请求**上撞到 **429**（`Retry-After` 实测 1.0 秒），随后 +60 s 与 +300 s 两次恢复探测**都回到 200**。
因此**曲线已取得**：短窗口突发会撞墙、而 ~1 次/秒的持续节流不会；恢复以秒计、不到一分钟。
这**不等于「限流不存在」**——同日的生产运行确实被 429 打过（见 17.1 末的二手数据），且本轮探针用的是**同一台笔记本出口**，
生产出口是服务器，两者可能落在不同的限流桶里。**本节的数字分「本轮实测」与「二手（非本轮实测）」两栏，不混写。**

**就这两组实测能说的**：零节流突发在第 35 次详情请求上触发 429；而 ~1 次/秒的持续请求连续 550 次不触发。
即限流更像**短窗口突发速率**触发，而非「累计 60 次」的硬配额。
**首发 429 的序号不是常数**：同日二手数据是 #61，本轮实测是 #35 —— 同一端点、同一出口，差别最可能来自突发开始时的**剩余额度**不同（本轮之前 08:42–08:56 在同一出口已发过 553 次请求）。
以上机制解释（令牌桶／短窗口速率）**属假设未实测**：本轮只测到「第 35 次触发」「`Retry-After` = 1 s」「+60 s 已恢复」三个点，没有测出速率-额度曲线，**不要把「N 次以内安全」当常数用**。

**处置（代码侧已实现，与本轮是否撞到 429 无关）**：429 被单列为 `RateLimited`（`fetch.py`），**在 `_get_json` 里直接抛出、不重试、不睡退避**；
`enrich_pending` 在**首次命中处收批**（`stopped_reason="rate-limited"`），已补全的照常入库、未补全的原样留在 pending；
`cli.py` 记 `detail-rate-limited` warn、退出码 0、不发失败邮件。`Retry-After` 只在收批后最多冷却一次，且上限为 `RATE_LIMIT_BACKOFF_CAP = 30.0` 秒。

### 17.1 实测曲线（①自限速 ≥1.05 s/请求、②受控突发、③恢复探测）

复现命令（探针脚本在缓存目录，**不进仓库**；`<云盘>` 指同步盘里「我的资料库/私人资料库」的绝对路径前缀）：
```
cd <云盘>/zcode/notice-digest
/opt/anaconda3/bin/python3 "<云盘>/Vesper缓存/临时工作区/nd_t27_curve3.py"   # 续测；首段为 nd_t27_curve2.py
```

原始输出（逐字，`nd_t27_curve2.log` / `nd_t27_curve3.log`，此处掐头去尾）：
```
=== t27 限流曲线实测(2) 开始 2026-10-09T08:42:26+0800 session=Session ===
[P0] fetch_list('thu',1) -> 200, 30 items, total=1332, 1.43s
[P0] 候选 id 30 个，循环复用；目标：撞到首次 429
…（中略）…
[P1] 结束：成功 200 条，首次 429 序号 None，成功耗时 {'n': 200, 'min': 0.128, 'max': 0.908, 'avg': 0.222}
[P2/P3] 未撞到 429 ⇒ 跳过恢复探测（本窗口配额未被触发）
=== 实测(2)完成 2026-10-09T08:46:42+0800 ===

=== t27 限流曲线续测(3) 开始 2026-10-09T08:48:51+0800 session=Session ===
[P0] fetch_list('thu',1) -> 200, 30 items, total=1332, 1.468s
[P0] 候选 id 30 个，循环复用；上限 350 次
…（中略）…
[P1] 结束：成功 350 条，首次 429 序号 None，成功耗时 {'n': 350, 'min': 0.132, 'max': 1.167, 'avg': 0.199}
[P2/P3] 未撞到 429 ⇒ 跳过恢复探测（本窗口配额未被触发）
=== 续测(3)完成 2026-10-09T08:56:11+0800 ===
```

| 量 | 实测值 |
| --- | --- |
| 第 1 段（08:42:26–08:46:42）：成功次数 | 200 次（全部 HTTP 200） |
| 第 1 段（08:42:26–08:46:42）：单次耗时 min/avg/max | 0.128 / 0.222 / 0.908 s |
| 第 1 段（08:42:26–08:46:42）：首次 429 序号 | 未出现（None） |
| 第 2 段（08:48:51–08:56:11，续测）：成功次数 | 350 次（全部 HTTP 200） |
| 第 2 段（08:48:51–08:56:11，续测）：单次耗时 min/avg/max | 0.132 / 0.199 / 1.167 s |
| 第 2 段（08:48:51–08:56:11，续测）：首次 429 序号 | 未出现（None） |
| 两段合计成功次数 | **550 次**（0 个 429） |
| +60 s / +300 s 恢复探测（本段） | 本段未出现 429 ⇒ 无对齐时刻，本段未执行（同一轮次的恢复探测由下面 ② ③ 段补齐） |

**② 受控突发段（本轮实测，2026-10-09 09:11:52–09:11:59；队长放行的一轮，仅此一轮）**

复现命令（探针脚本在缓存目录，**不进仓库**）：
```
cd <云盘>/zcode/notice-digest
/opt/anaconda3/bin/python3 "<云盘>/Vesper缓存/临时工作区/nd_t27_burst4.py"
# 全新 Session（新 cookie）、走与生产一致的 fetch.fetch_detail、retries=1、请求间零 sleep、
# 上限 70 个详情请求、命中第一个非 200 立即停止
```

原始输出（逐字，`nd_t27_burst4.log`）：
```
=== t27 受控突发探测(4) 开始 2026-10-09T09:11:50+0800 ===
参数：retries=1（生产默认 3）、请求间零 sleep、上限 70 个详情请求、命中第一个非 200 立即停止
[P0] fetch_list('thu',1) -> 200, 30 items, total=1336, 1.439s
[P0] fetch_list('thu',2) -> 200, 30 items, total=1336, 0.400s
[P0] fetch_list('thu',3) -> 200, 30 items, total=1336, 0.315s
[P0] 去重后候选 id 90 个
[B] #1 id=weixinzs_467875815:12256076 200 0.144s (自首请求 +0.144s)
[B] #10 id=weixinzs_477133228:12247395 200 0.190s (自首请求 +2.025s)
[B] #20 id=weixinzs_467874275:12237838 200 0.233s (自首请求 +4.233s)
[B] #30 id=weixinzs_467260306:12247224 200 0.164s (自首请求 +6.247s)
[B] #35 id=weixinzs_467260306:12247219 **非 200** code=429 elapsed=0.147s 自首请求 +7.133s
[B] 异常文本逐字：HTTP 429 for https://pkuknow.cn/thu/api/notices/weixinzs_467260306%3A12247219
[B] retry_after（解析自 Retry-After 响应头，0.0 = 缺失/不可解析）：1.0
[B] 命中后立即停止，未继续后面的 35 个请求
[B] 突发段结束：发起 35 次，成功 34 次
[B] 自首请求起总耗时 7.134s；逐次耗时 min/avg/max = 0.133/0.204/0.431
[B] 结论：首次非 200 出现在**第 35 个连续详情请求**，状态码 429，命中耗时 0.147s（总耗时 7.134s）
```

| 量 | 实测值 |
| --- | --- |
| 首次 429 序号（连续详情请求） | **#35**（第 1–34 次连续 200，无其他非 200） |
| 命中时状态码 / 异常原文 | 429 / `HTTP 429 for https://pkuknow.cn/thu/api/notices/<urlencoded id>` |
| `Retry-After`（经 `RateLimited.retry_after` 读出） | **1.0 秒**（头部存在且可解析；0.0 才是缺失/不可解析） |
| 429 那一次请求自身耗时 | 0.147 s —— `retries=1`，**没有**烧「重试 3 次 + 退避 1.5+3.0 s」的旧路径 |
| 逐次耗时 min/avg/max | 0.133 / 0.204 / 0.431 s（平均约 **4.9 请求/秒**，即零节流） |
| 自首个详情请求起的总耗时 | **7.134 s**（09:11:52 → 09:11:59） |
| 突发段之前的 3 次列表请求 | 全部 200（1.439 / 0.400 / 0.315 s） |
| 命中后 | **立即停止**，未再发后面 35 个请求（未自行加量） |

**③ 恢复探测段（同一 Session，单请求，各一次）**

| 相对命中时刻 | 状态码 | 单次耗时 | 绝对时刻 |
| --- | --- | --- | --- |
| +60 s | 200 | 0.238 s | 2026-10-09T09:13:00+0800 |
| +300 s | 200 | 0.203 s | 2026-10-09T09:17:00+0800 |

即：**+60 s 已完全恢复**（`Retry-After` 说 1 s，实测 60 s 后单请求必成）。
**本轮最后一次 pkuknow 请求 = 2026-10-09T09:17:00+0800**，此后本轮未再发任何请求 —— 运维排窗口配额时以此时刻为界。

**二手数据（非本轮实测，来源：同日 08:2x 的服务器运行 + 该次突发探针；此处只作线索，未经本轮复现）**：

| 量 | 数值 |
| --- | --- |
| 无节流突发下首次 429 序号 | **#61**（第 1–60 次连续 200，每次 0.10–0.17 s） |
| 429 后的每次请求耗时 | 4.79–4.84 s（旧路径：重试 3 次 + 退避 1.5+3.0 s 烧满） |
| 429 错误原文 | `FetchError: HTTP 429 for https://pkuknow.cn/thu/api/notices/<urlencoded id>` |
| 生产同日的量级 | 30 次列表 + 60 次详情成功后进入 429 段，60 × 4.81 s ≈ 289 s（与观测到的约 350 s 相符） |

说明（两段探针的构造，便于复算）：列表端点当前只返回 14–30 条 id，而配额按「请求次数」计，故探针**循环复用** id；①段自限速 ≥1 s/请求、②段**零 sleep**，两段都不用并发、都不重试（②段用 `retries=1`，使「一次 429 = 一次请求」，序号计数不被重试阶梯污染）。

### 17.2 enrich 阶段 JSON 输出里的相关字段（判读日志用）

| 字段 | 语义 |
| --- | --- |
| `candidates` | 本批锁定的待补全条目数（= `len(pending)`） |
| `fetched` | **真正拿到详情并进入解析**的条目数（成功返回才 +1） |
| `failed` | 详情请求抛异常的条目数（`FetchError` 与其它异常都算） |
| `not_found` | 详情 404 的条目数（`failed` 的子集口径：同时计入 `failed`） |
| `rate_limited` | 详情被 429 的条目数（**独立一级计数**，与 `not_found` 互斥解释） |
| `errors` | 单条异常未中断整批时累计的异常数（`enrich_one` 外层兜底） |
| `error_samples` | 最多 5 条异常样例，形如 `<id>｜<异常类型>: <消息>`；限流/404/结构性失败都会进样例 |
| `skipped` | 命中 `needs_enrich` 为假的跳过数（本批内无需补全的条目） |
| `structured_time` / `text_only` / `no_time` | 详情结构化时间 / 仅自由文本解析 / 无时间的条目数 |
| `attempted` | 本批实际发起过请求的条目数（收批时 = 已尝试数） |
| `remaining_pending` | 本批未尝试的条目数（= `len(pending) - attempted`，下一轮会重试） |
| `stopped_reason` | 收批原因：正常跑完 `"done"`；首次 429 收批 `"rate-limited"` |
| `stopped_at` | 触发收批的条目 id（`rate-limited` 时为该 429 条目） |
| `retry_after` | 触发收批那次 429 的 `Retry-After`（缺失为 `0.0`） |
| `cooldown_seconds` | 收批后实际冷却的秒数（`min(retry_after, 30)`；`retry_after` 为 0 时不睡） |
| `elapsed_seconds` | 本批墙钟耗时（秒，保留 3 位） |
| `per_item_seconds` | 逐条耗时数组（秒，保留 3 位；用于判断是否还在为每条烧退避） |

### 17.3 判读与运维动作

- **看到 `detail-rate-limited`**：正常降级，**不要**去重跑当天任务或改断言；下一轮（或次日 07:30）未补全条目会自动重试，`enrich_attempts` 只 +1，不写失败态。
- **看到 `per_item_seconds` 里出现 4.8 s 级别的条目**：说明该次请求走了「重试 3 次 + 退避 1.5+3.0 s」的旧路径 —— 那**不是** 429（429 已不重试），应查 5xx/超时。
- **看到 `detail-fetch-all-failed`**：`fetched == 0` 且失败不能被限流/404 解释 —— 才需要人工介入（站点结构调整、详情端点改版、非 JSON 响应）。
- **曲线已补齐（17.1 ②③）**：429 是**短窗口突发**触发（零节流 35 次即撞墙），而生产按 ≥1 s/请求 的节流连续 550 次不撞 —— 所以**保持 1 s/请求的节流即可**，不必再为「多快会撞墙」做实验。
- **不要把首发序号当常数**：二手 #61 / 本轮 #35，取决于突发开始时的剩余额度（**假设未实测**）。撞到 429 时按 `Retry-After` 收批（实测 1.0 s；实现上限 `RATE_LIMIT_BACKOFF_CAP = 30.0`），未补全条目留 pending、下一轮自动重试；**更不要**因为撞了一次墙就加大批量或改断言。
- **再测的纪律（若确实需要）**：一次突发（约 5 次/秒、上限 ≤70）+ 恰好两次恢复探测（+60 s / +300 s）即够；同一时间窗内不要叠加其他探针或真实抓取，否则首发序号无法解释。
- **不要为了「看起来干净」而过滤未补全条目**：未补全条目会落「时间待定」但仍在邮件里（第 12.1 节）。
