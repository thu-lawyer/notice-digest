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
| 5 | **反馈按钮的渲染只判「基址是否为空」，不判是否回环** | `ND_FEEDBACK_BASE` **为空**（或 `ND_HMAC_SECRET` 为空）才不渲染 👍/👎；**配成回环地址时按钮照渲染，href 指向 `127.0.0.1`** | 比「没有按钮」更糟：用户点得下去，但点开必然打不开（2026-10-09 当天就是这样） | 生产 `.env` 必须同时设置 `ND_FEEDBACK_BASE`（**外部可达、非回环**）与 `ND_HMAC_SECRET`；判据与证据见第 15、18.6、18.8 节 |

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

前置事实（2026-10-09 复核，**推翻本文档早先的写法**）：`/opt/notice-digest/.env` 的 `ND_FEEDBACK_BASE` 是**回环地址、但非空**，而 `render.py::_feedback_links`（L208–L217）只判「基址是否为空」与「`hmac_secret` 是否为空」——**全文件没有 loopback / 127.0.0.1 特判**（`grep -niE 'loopback|127\.0\.0\.1|localhost'` 零命中）。因此生产配置下**邮件照常渲染 👍/👎 与标题点击链接，href 全部指向 `http://127.0.0.1:8791/nd/...`**；用户点开的是自己电脑的回环地址，必然无效。**这不是「安全降级」，是渲染了一条用户侧必然打不开的链接。** 要让反馈回路可用，须先决定公网基址：

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

## 18. 反馈入口公网开通：`nd.thulaw.top` 的实测状态、硬前置与逐类返回码（t32）

**结论先行（2026-10-09 10:47 更新）：公网入口仍未开通。** 第一道硬前置（DNS A 记录）**已解决**：`nd.thulaw.top` → `<服务器公网 IP>`，服务器本地 `getent`、`@114.114.114.114`、`@1.1.1.1`、权威 NS `@dns15.hichina.com` 四处一致。但随即暴露**第二道硬前置：域名未 ICP 备案** —— 阿里云在边缘按 Host/SNI 拦截该域名（80 返回备案拦截页、443 直接重置，详见 §18.8）。因此 **§18.3 的四步目前一步都走不了**：`certbot --nginx` 的 HTTP-01 挑战已被实测否决（staging 原文见 §18.3），`.env` 也**刻意没有**切 `https://nd.thulaw.top`（切了只会让下一封邮件渲染出打不开的按钮）。所有与域名无关的准备（vhost 复核、限流区复核、certbot 可用性、服务重启与健康检查、逐类返回码实测、零写信证明）**已完成并留证**。

### 18.1 公网拓扑（现状，逐字对照磁盘）

```
邮件客户端 / 浏览器
  └─ https://nd.thulaw.top/nd/{f,c,health}      ← 待 certbot 签发后才成立
       └─ nginx: server_name nd.thulaw.top        /etc/nginx/sites-enabled/notice-digest.conf
            location /nd/                         limit_req zone=nd_feedback burst=20 nodelay;
                                                  limit_req_status 429;
                                                  proxy_pass http://127.0.0.1:8791;
            location /                            return 404
       └─ notice-feedback.service (Type=simple)   仅监听 127.0.0.1:8791
            基址来自 /opt/notice-digest/.env 的 ND_FEEDBACK_BASE
```

限流区在 `/etc/nginx/conf.d/notice-digest-ratelimit.conf`（`limit_req_zone` 属 http 上下文、`location` 属 server 上下文，故必须拆成两个文件）。**复核结果（2026-10-09）**：vhost 目前只有 `listen 80`（无 443 段，certbot `--nginx` 会补），`nginx -t` = syntax is ok / test is successful；`location /nd/` 的反代目标与 `Host` / `X-Real-IP` / `X-Forwarded-For` / `X-Forwarded-Proto` / `User-Agent` 头均正确。

### 18.2 DNS 是硬前置（两轮独立实测，当时均无记录；**A 记录已于 2026-10-09 生效**）

| 探测位置 | 命令 | 结果 |
| --- | --- | --- |
| 服务器（本地 resolver） | `getent hosts nd.thulaw.top` | 无输出 |
| 服务器 → 8.8.8.8 / 223.5.5.5 / 119.29.29.29 / 127.0.0.53 | `dig +short @<ns> nd.thulaw.top A` | 全部为空 |
| 本机 macOS | `host nd.thulaw.top` | `Host nd.thulaw.top not found: 3(NXDOMAIN)` |
| 对照（同命令、同机器） | `thulaw.top` / `blog.thulaw.top` | 均 → `<服务器公网 IP>`（说明解析链路正常，缺的只是这条记录） |

域 `thulaw.top` 的 NS 是 `dns15.hichina.com` / `dns16.hichina.com`（阿里云/万网 DNS 控制台）。**要在该控制台加一条 `nd` 的 A 记录指向 `<服务器公网 IP>`**；生效判据就是上表第一行命令出现结果。**不要**在记录存在前跑 certbot —— HTTP-01 挑战域名不可解析必然失败，且可能留下需要清理的临时状态。

### 18.3 记录生效后的四步（**当前被 ICP 备案拦截，暂不可执行 —— 先读 §18.8**）

```bash
# ① 签发证书（--nginx 会自动在 vhost 内补 443 段并加 80→443 跳转）
certbot --nginx -d nd.thulaw.top

# ② 换公网基址（旧值为 http://127.0.0.1:8791，先留备份）
cd /opt/notice-digest && cp -a .env /root/nd-env-backup-$(date +%Y%m%dT%H%M%S).bak
sed -i 's|^ND_FEEDBACK_BASE=.*|ND_FEEDBACK_BASE=https://nd.thulaw.top|' .env

# ③ 只重启反馈服务（这是唯一可安全重启的 unit）
systemctl restart notice-feedback.service && systemctl is-active notice-feedback.service

# ④ 从公网验证（必须从外部，见 §18.5 的启动自检误报）
curl -sS -o /dev/null -w '%{http_code}\n' https://nd.thulaw.top/nd/health   # 期望 200
curl -sS -w ' [%{http_code}]\n' https://nd.thulaw.top/nd/f                  # 期望 400 bad kind
```

**红线**：绝不重启 `notice-digest.service`（oneshot，重启即真实发信），绝不运行任何 `send` 子命令。改完 `.env` 只重启 `notice-feedback.service`。

### 18.4 `/nd/f`、`/nd/c`、`/nd/health` 的逐类返回码（2026-10-09 实测；经 nginx 与直连 8791 两条路径结果一致）

对应实现 `notice_digest/feedback.py` 的 `do_GET`，校验顺序为 **path → 参数 → 签名 → 过期 → 预取 → 写入**。

| 请求 | 实测返回 | 说明 |
| --- | --- | --- |
| `GET /nd/f`（无参数） | **400 `bad kind`** | 参数校验先于签名校验 |
| `GET /nd/f?id=x`（`k` 缺失） | **400 `bad kind`** | 同上 |
| `GET /nd/f?id=x&k=zz&t=<32hex>`（`k` 非法） | **400 `bad kind`** | 合法 `k` 仅 `up`/`down`（`click` 只在 `/nd/c` 与内部学习路径使用） |
| `GET /nd/f?id=x&k=up`（无 `t`） | **403 `bad signature`** | 签名缺失 |
| `GET /nd/f?id=x&k=up&t=<错误签名>`（含旧密钥签的） | **403 `bad signature`** | 签名错误 |
| `GET /nd/f?…&exp=<过去时刻>&t=<正确签名>` | **403 `expired`** | 本轮未单独发请求；按实现与既有口径记录 |
| `GET /nd/f?id=<真实条目>&k=up&t=<正确签名>`，UA 命中预取名单（如含 `bot`） | **202** + 头 `X-ND-Skipped: prefetch` | **不写库、不计分**；stderr 记一行中文日志（见 §18.5） |
| 同上，但 **无 UA** | **202** + `X-ND-Skipped: prefetch` | 「无 UA 即预取」是显式规则 |
| 同上，但 UA 是真人/通用客户端（curl 默认、`python-urllib` 等） | **200** | 这一步**会真正落一条 feedback 行**；本轮刻意未执行，避免往生产库塞试探数据 |
| `GET /nd/c?id=x`（无 `t`） | **403 `bad signature`** | |
| `GET /nd/c?id=<不存在的 id>&t=<正确签名>` | **404 `unknown item`** | 在**任何写入之前**返回，故是无需写库的阳性对照 |
| `GET /nd/health` 或 `/healthz`（无需签名） | **200** `ok` | |
| `GET /nd/<其它路径>` | **404 `not found`** | |
| `GET /`（`Host: nd.thulaw.top`） | **404**（nginx 自身页面） | `location / { return 404; }` |

**零写库证明**：上表全部请求跑完后 `select count(*) from feedback` 仍为 **0**（含签名正确但被判为预取的两条 202）。即**所有未签名、签名错误、预取的请求都不产生任何写入** —— 这正是第 14 节那个判据：实现返回码与旧契约文字不符时，先看被保护的性质是否成立，成立就是措辞缺陷、不是代码缺陷。

### 18.5 两个会误导排查的已知现象

1. **启动自检的「不可达」警告是竞态误报**：`notice-feedback.service` 每次启动都会自检 `ND_FEEDBACK_BASE` 并打印 JSON（`public_base` / `reachable`）。2026-10-09 重启后它报 `http://127.0.0.1:8791/nd/health -> URLError: [Errno 111] Connection refused`，并打一行「警告：配置的公网反馈地址不可达」—— 但**同一 URL 在启动完成后 curl 是 200**：自检发生在监听器就绪之前。**换成 `https://nd.thulaw.top` 后每次重启都会重现这条警告，不要据此判定开通失败**；公网是否可达只能由**外部** `curl https://…` 判定。
2. **预取日志是中文，不含 ASCII `prefetch`**：stderr 文案为 `[feedback] /nd/f 判定为预取，已忽略（未计入偏好）：item=… kind=… ua=…`。用 `journalctl -u notice-feedback.service | grep prefetch` 会**搜不到**（本轮就因此误判过一次「日志没写出来」），要搜 `预取`；ASCII 的 `prefetch` 只出现在响应头 `X-ND-Skipped` 里。日志随写随出，无缓冲延迟（实测请求时刻 10:37:44 与日志行时间戳一致）。

### 18.6 今天那封已投递邮件里的反馈链接：全部无效（符合预期）

- 今天 09:36:51 的 `notice-digest.service` 运行投递时，`ND_FEEDBACK_BASE` 是 `http://127.0.0.1:8791`（**回环但非空**），`ND_HMAC_SECRET` 也已设置 ⇒ `_feedback_links` 的两道条件都满足，**邮件里照常渲染了 👍/👎 与标题点击链接，href 全部指向 `http://127.0.0.1:8791/nd/...`**；用户点开的其实是自己电脑的回环地址，必然打不开 —— 这正是用户反馈的「按钮点击无效」。（**本文档早先写的「走安全降级、邮件里没有按钮」是误读**：判据只有「`feedback_base` 是否为空」+「`hmac_secret` 是否为空」，无任何回环特判；见第 12 节第 5 项与第 15 节。）
- 另外当天 09:56 轮换了 `ND_HMAC_SECRET`（任务 t30），因此任何**此前**签发过的 `/nd/f`、`/nd/c` 链接签名均已失效（点击得 403）。两者叠加的净效果：**今天邮件里的反馈按钮不产生任何反馈写入**，无用户可见影响。
- 今天那封邮件**不代表**反馈回路已可用：只有 §18.3 的 ②③ 做完，**下一封**邮件才会第一次带可用的反馈链接。而 §18.3 目前被**备案**卡住（§18.8），所以下一封邮件仍会带上**同样不可达**的按钮。

### 18.7 上一轮完成 / 未做（当时等 DNS）与留证

| 项 | 状态 | 证据 |
| --- | --- | --- |
| vhost / 限流区复核 | 已完成 | `nginx -t` 通过；配置见 §18.1 |
| `notice-feedback.service` 重启 + 健康检查 | 已完成 | 重启用 10:37:58（InvocationID `4f17c22c2f1c4bdbbbdd4a0fc7d9bea4`）；`127.0.0.1:8791/nd/health` = 200；经 nginx（`Host: nd.thulaw.top`）= 200 |
| 逐类返回码实测 | 已完成 | §18.4 表；跑完后 `feedback` 行数仍为 0 |
| certbot 可用性 | 已确认待用 | `certbot --version` = 2.9.0；`certbot certificates` = No certificates found（全新，无历史证书需处理） |
| **A 记录 / 证书 / `ND_FEEDBACK_BASE` 切换 / 公网验证** | **未完成（DNS 已解，改由 ICP 备案卡住，见 §18.8）** | §18.2 两轮探测当时均为空（**A 记录已于 2026-10-09 生效**，随即撞上未备案拦截）；`ND_FEEDBACK_BASE` 旧值仍为 `http://127.0.0.1:8791`（**刻意不提前切换**：https 侧链接当前必然打不开，改成 https 会让下一封邮件渲染出同样不可用的按钮） |
| 全程零真实发信 | 已完成 | `data/send-receipts/` 文件数 7 → 7（增量为 0）；`notice-digest.service` 始终 `inactive/dead`，`InvocationID` 仍为 `fada3152d0764aca9c52f027f57fa61f`、`ExecMainStartTimestamp` 仍为 09:36:51，无新运行 |

### 18.8 第二道硬前置：域名未 ICP 备案（阿里云按 Host/SNI 拦截）—— 2026-10-09 实测

**结论：DNS 解决后暴露的是备案问题，与 nginx / certbot / 本机配置全都无关。** 决定性判据是「同一个 nginx、同一个 IP，**换 Host / SNI 就换结果**」：

| 探测（**均从公网发起**，非服务器本机） | 命令 | 实测结果 |
| --- | --- | --- |
| 域名 + 80 | `curl --resolve nd.thulaw.top:80:<服务器公网 IP> http://nd.thulaw.top/nd/health` | **403**，`Server: Beaver`，正文标题 `Non-compliance ICP Filing`（请求**没到** nginx） |
| 同机其它子域 + 80 | 同上，Host 换 `blog.thulaw.top` / `thulaw.top` / `comments.thulaw.top` | **同为 403** ⇒ 是整个域的备案状态，不是 `nd` 这条记录的问题 |
| ACME 挑战路径 + 80 | `GET http://nd.thulaw.top/.well-known/acme-challenge/<token>` | **403** 同一个拦截页 ⇒ **HTTP-01 不可能通过** |
| 域名 + 443（带 SNI） | `curl --resolve nd.thulaw.top:443:<服务器公网 IP> https://nd.thulaw.top/nd/health` | **000 / `Recv failure: Connection reset by peer`**（TCP 连上、ClientHello 发出后被重置），3 次取样稳定 |
| SNI 换 `blog.thulaw.top` / `thulaw.top` | 同上 | **同样被重置**（而 `blog` 的自签 443 vhost 在服务器本机 `curl -k` 是 200 ⇒ 配置无问题，问题在链路上） |
| **不带 SNI**（裸 IP） | `curl -k https://<服务器公网 IP>/` | **200**（命中 nginx 默认 443 站点）⇒ 拦截是按 **SNI / Host** 做的 |
| 裸 IP + 80 / 裸 IP + 8080 | `curl http://<服务器公网 IP>/nd/health`；`curl http://<服务器公网 IP>:8080/` | **均 200** ⇒ 未备案拦截**只针对域名**，不针对 IP |
| 服务器本机（对照组） | `curl -H 'Host: nd.thulaw.top' http://127.0.0.1/nd/health` | **200** ⇒ vhost 与反代本身完全正常 |
| Let's Encrypt staging（**境外**） | `certbot certonly --nginx -d nd.thulaw.top --dry-run …` | **同样 403**（原文见 §18.3）⇒ 拦截与客户端所在地无关 |

**推论（三条，缺一不可）**：
1. **只要备案状态不变，`https://nd.thulaw.top` 对任何人都打不开** —— 不管有没有证书；因为 TLS 握手的 ClientHello（带该 SNI）在阿里云边缘就被重置了。
2. **`certbot --nginx` 也永远签不出证书**：certbot 2.9.0 的 `nginx`/`standalone`/`webroot` 三个认证器都只走 HTTP-01，而 80 端口被备案拦截页接管；`tls-alpn-01` 只出现在 `manual` 插件里（DNS/HTTP 手工模式），不能用 `--nginx` 走。**不要反复重跑 certbot**（每次都会注册/使用账户并消耗 Let's Encrypt 的失败限额）。
3. 因此 `.env` 里 `ND_FEEDBACK_BASE` **维持回环值不动**是当前唯一正确选择：改成 `https://nd.thulaw.top` 只会让下一封邮件渲染出**必然打不开**的按钮，比现状更糟。

### 18.9 三条可行路径（均需用户拍板）

| 路径 | 做法 | 代价 / 风险 |
| --- | --- | --- |
| **A 完成 ICP 备案（推荐）** | 在阿里云备案控制台为 `thulaw.top` 提交备案；通过后 80/443 自动解封 | 周期以周计、需用户实名与域名材料；**唯一能让 §18.3 四步原样跑通的路径** |
| B 换已备案域名 / 境外主机 | 把 `/nd/` 反代放到某个已备案域名的站点下，或放到境外主机 | 需用户决策；多一份运维面（且需重新配 DNS 与证书） |
| C IP 直连（临时可用，不推荐） | `ND_FEEDBACK_BASE=http://<服务器公网 IP>/...`（裸 IP 不被拦截，实测 200） | **明文 HTTP**，反馈令牌在链路上可见；Host 会落到默认站点，需另加一条按 IP 匹配的 `location /nd/`。属基础设施变更，需用户显式同意 |

### 18.10 本 attempt（t32 attempt 2）做了什么 / 没做什么

| 项 | 状态 | 证据 |
| --- | --- | --- |
| DNS 生效复核 | 已完成 | 服务器 `getent hosts` / `@114.114.114.114` / `@1.1.1.1` / 权威 `@dns15.hichina.com` 四处一致 → `<服务器公网 IP>` |
| HTTP-01 可行性裁决 | 已完成（结论：不可行） | staging dry-run 原文 + §18.8 证据表（含「无 SNI 得 200」这个决定性对照） |
| 备案拦截取证 | 已完成 | §18.8 全表（9 行探测，公网发起） |
| `ND_FEEDBACK_BASE` 切 https | **未做（有意不做）** | 旧值 `http://127.0.0.1:8791` 原样保留；`.env` sha256 `404e11f8e218ee2e0c4d3a21bae18ad9b7d77b7692b00cf0b3c7c7f301b71bc4` 前后一致（未做备份也不需要备份，因为没改） |
| `notice-feedback.service` | 上一轮已重启（本轮无需再动） | `127.0.0.1:8791/nd/health` = 200；`notice-digest.service` 全程未被触碰 |
| 公网 `https://…/nd/health` = 200 | **未达成** | 443 被 SNI 重置（见 §18.8） |
| 零真实发信 | 已完成 | `data/send-receipts/` 7 → 7（本任务全程 0 增量）；`notice-digest.service` 仍 `inactive/dead`，`InvocationID` 仍为 `fada3152d0764aca9c52f027f57fa61f`、`ExecMainStartTimestamp` 仍为 09:36:51 |
| staging 演练的残留物 | **已披露** | 新增 `/etc/letsencrypt/accounts/acme-staging-v02.api.letsencrypt.org/`（staging 账户，惰性，未用于任何签发）、`options-ssl-nginx.conf`、`ssl-dhparams.pem` 与两个 `.updated-*` 摘要文件；**未产生任何生产账户或证书**（`/etc/letsencrypt/live` 不存在）；nginx vhost md5 前后一致 `3ebf4351a100c50d86c9aa67e366d7a0`；`nginx -t` 通过 |

**下一轮从哪继续**：备案通过（或用户选定 B/C）后直接回到 §18.3 —— 注意首次签发需显式给邮箱或加 `--register-unsafely-without-email`（当前 `/etc/letsencrypt` 里**没有任何生产账户**，「复用现有 certbot 账户邮箱」无对应物）。

## 19. 邮件分节改为网站原生分类并每类保底展示（t31）

**结论先行**：邮件顶级分节不再用自创的 5 个桶（今日可去 / 讲座 / 实习 / 文体 / 其他），而是**逐字采用 pkuknow 站点原生分类**（`items.category`）；当日**有内容的分类才渲染**（不渲染空节），组内按个性化分降序，单组最多 20 条；紧迫度（今天 / 明天 / 截止）**降级为条目标签**，不再是分节维度。

### 19.1 顶级分节 = 站点原生分类（13 类）

`notice_digest/render.py` 的 `CATEGORY_ORDER` 是**唯一权威顺序**（按站点体量降序）：

校园动态 → 实习就业 → 学术科研 → 社团公益 → 讲座活动 → 文体活动 → 学习成长 → 生活资讯 → 院系资讯 → 交流访学 → 校园服务 → 学业教务 → 奖助评优

- **有内容才渲染**：`group_by_category()` 先分桶，只渲染非空桶；当日没有讲座就不出现「讲座活动」标题。
- **category 缺失 / 空串 / 未知值**统一落 `UNCATEGORIZED = "未分类"`，该节固定排在 13 类之后（同样只在有内容时出现）。
- **要加第 14 类**：在 `CATEGORY_ORDER` 里加一项即可（列表顺序即渲染顺序）—— 分节标题、分桶、排序全部由这一个常量派生，**没有第二处清单**。不要再去改 `Config.sections`（见 19.3）。

### 19.2 组内排序、单组上限与超限提示

- 组内按个性化分降序：`score` 空值按 0 处理。
- 单组上限 `GROUP_LIMIT = 20`；超出时在该组列表**末尾**补一行 `本组还有 N 条未列出`（模板 `GROUP_OVERFLOW_TMPL`，N = 被隐藏条数）。截断是**每组独立**的，不是全局投影。

### 19.3 全局「其他新通知」折叠桶已删除；`top_n` 仍生效但作用点在上游

- 旧的全局折叠桶（把塞不进 5 个桶的条目合并成一节）**已删除**：邮件里不再有「其他新通知」这类节。
- `Config.sections` 现在是**过时字段**：渲染器不再读它（分节只认 `CATEGORY_ORDER`），保留只为兼容既有配置文件与 `tests/test_score.py` 的旧断言。**改分节不要改它。**
- `Config.top_n` 仍然有效，但作用点已上移到 `cli.py` 的 `_build_report()`（`scored[:top_n]`）：即「先按总分截断，再分节渲染」。因此 `top_n` 变小会让某些分类**整体消失**——这是预期语义，不是缺陷。

### 19.4 紧迫度改为条目标签

`_urgency_tags(p, now)` 生成，拼在条目 meta 行前部：

| 条件 | 标签 |
| --- | --- |
| `start` 距当天 0 天 | 【今天】 |
| `start` 距当天 1 天 | 【明天】 |
| 无 `start` 但 `bucket == "today"` | 【今天】 |
| 有 `deadline` | 【截止 MM-DD】 |

- 「今天 / 明天能去」「截止提醒」**不再是顶级分节**（旧分节名已从代码移除）。
- `时间待定（原文：…）` 的语义与措辞**未变**：仍是无解析结果时的兜底展示。

### 19.5 未受影响的部分

- 邮件 HTML 结构（`nd-section` / `nd-item` / `data-nd-item="1"`）、主题行格式、meta 行与页脚文本、零新增日的 `("", "", "")` 哨兵、无新闻日 / 详情全 429 的优雅降级、结构性故障 exit 3 + 失败邮件 —— **均未改动**。
- **ICS 输出与分节无关**：日历事件只取决于条目的显式 `start`，分节方式不影响 ICS 字节。

### 19.6 验证方式

- `tests/test_render.py` 覆盖：原生分类固定顺序、组内按分类归属、缺席分类不渲染空节、未知/空/缺失分类落「未分类」、组内按个性化分降序、超限提示出现在第 20 条之后且位于 `</section>` 之前、`top_n` 不改变分节逻辑、紧迫度只作条目标签、无全局折叠桶。
- **反证（在项目目录外的副本里注入缺陷，被保护断言必须变红）**：`_group_note` 恒返回空串 → 超限提示断言红；`_urgency_tags` 恒返回空串 → 两条紧迫度断言红；`category_of` 恒返回「未分类」→ 原生分类分节断言红；`group_by_category` 的 `reverse=True` 改 `False` → 组内排序断言红。四项均实测变红、对照组保持绿，断言非空转。

> 脱敏说明：本文档随公开仓库 thu-lawyer/notice-digest 发布，服务器公网 IP 一律以 `<服务器公网 IP>` 占位（域名与端口保持原样，便于对照拓扑）；复现命令时把你实际的服务器 IP 代入即可。
