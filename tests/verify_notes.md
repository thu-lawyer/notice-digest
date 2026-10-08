# t3 独立验证记录 · notice-digest

验证人：`verifier`（AgentTeams 班组 notice-digest）
覆盖轮次：**round 1 = t3（attempt 2，§0–§14）**；**round 2 = t9（§15）**；**round 3 = t11（§16，D-3 关闭复验）**
被验证对象：`zcode/notice-digest`（**只读**，三轮均未改动 `notice_digest/` 任何源码）
验证脚本：`tests/test_integration.py`（t3 新建、t9 追加 round-2 用例、t11 追加 round-3 用例，stdlib-only unittest，
  截至 round 3 共 **37 项**，1900+ 行；`discover -s tests` 全量含既有单元测试共 **157 项**）
解释器：`/opt/anaconda3/bin/python3`（3.12.7）
日期：2026-10-08

> **脱敏约定**：本文档中所有敏感字面量一律用字符类打断（如 `39[.]105[.]73[.]34`、
> `@tsinghua[.]org[.]cn`、`李博[冉]`、`授权[码]`），目的是让本文档本身**不会**被
> 同一条凭据扫描规则命中。照抄命令时请自行还原。

---

## 0. 结论摘要

| # | 验收项 | 判定 | 一句话理由 |
|---|---|---|---|
| ① | 活体 API 全新空库端到端（fetch→enrich→report→render→send --dry-run） | **pass** | 真站点真数据，180 条入库、6 个产物落盘、ICS 独立校验通过 |
| ② | ICS 独立合规校验（自写脚本、不复用被验证模块） | **pass** | CRLF / ≤75B 折行 / BEGIN-END 配对 / UID 唯一 / VALARM / TZID 全部通过 |
| ③ | 证伪 1：幂等与去重（无静默丢条） | **pass** | 二次 fetch `new=0`、行数不变；站点 60 唯一 id 全部入库，缺失 0 |
| ④ | 证伪 2：学习方向/上下界/不串扰/不重复施加 | **pass** | 👍 升 👎 降、200 次后仍在 ±5 内、5 个无关特征未变、重复反馈 no-op |
| ⑤ | 证伪 3：失败模式不得静默成功 | **suspect** | SMTP/凭据/404/签名全部显式失败；但 `items=null` 走 exit 0（见 D-1）〔历史：该模式依据的「page ≥ 43 起 `items` 为 null」前提已被 t8 证伪，见 §15.1〕 |
| ⑥ | 证伪 4：零配置可跑（无 profile.yaml / .env） | **pass** | 活体 fetch→render 成功，回落内置权重，无已学权重 |
| ⑦ | 证伪 5：无新条目则不发信 | **pass** | 空库不产信不写台账；台账已记当日后再跑仍不写 |
| ⑧ | 凭据/隐私扫描（服务器 IP/邮箱/姓名/SMTP 账号） | **suspect** | 工作树 0 命中；但 `.env.example` **不存在**，占位符模板无从校验 |
| ⑨ | 产出本验证记录（含命令/原始输出/判定/复现步骤） | **pass** | 即本文档 |

**测试套件实测结果**：`Ran 22 tests ... FAILED (failures=1, skipped=1)` —— 20 通过、
1 失败（**真实缺陷探针** `test_04f`，见 D-2）、1 跳过（`.env.example` 不存在）。

**一条 blocker 级实现缺陷**（D-2）：HTTP 反馈接口的合法签名请求会因
`sqlite3.ProgrammingError`（SQLite 连接跨线程）而崩溃 → 邮件内 👍/👎 这条生产反馈通道
不可用。已独立复现两次，详见 D-2。

---

## 1. 验收①：活体 API 全新空库端到端

**命令**（契约 verify #1，逐字）：
```bash
cd "../zcode/notice-digest" && /opt/anaconda3/bin/python3 -m unittest tests.test_integration -v
```
**实质证据**（`data/t3_unittest.log` 原始输出）：
```
[E2E] fetch new=60 pages=2 stop=reached-max-pages | enrich={'candidates': 3, 'fetched': 3, 'failed': 0, 'skipped': 0} | db_items=60 (stats.total_items=60)
[E2E] dry-run 产物 6 个：
    t3/outbox/20261008-200437-email.eml  41332 bytes
    t3/outbox/20261008-200437-email.html  17141 bytes
    t3/outbox/20261008-200437-email_plain.txt  7612 bytes
    t3/outbox/20261008-200437-events.ics  1891 bytes
    t3/outbox/20261008-200437-meta.json  236 bytes
    t3/outbox/20261008-200437-subject.txt  46 bytes
```
**判定**：**pass**。
- 全新空库（`data/t3/e2e_t3.db`，测试开头删除重建），无 mock、无固定 fixture；
  真实请求 `https://pkuknow.cn/thu/api/notices?page=N`，限速 ≥1 req/s。
- 链路每一步都有**真实产物**：DB 行数（`stats.total_items=60`）、6 个落盘文件及字节数。
- 独立复跑（契约 verify #2，`data/e2e.db`，`--pages 6`）：
  ```
  {"campus":"thu","pages_scanned":6,"new":180,"known":0,"stop_reason":"reached-max-pages","db":"data/e2e.db",...}
  {"candidates":5,"fetched":5,...}
  items=180  with_detail=5  feedback=0  sends=0
  分桶统计：今天=2, 明天=2, 待定=16
  ```
  即 6 页 × 30 条 = 180 条全部入库，与 API 一致。

## 2. 验收②：ICS 独立合规校验

**独立性声明**：校验函数 `validate_ics_bytes()` 在 `tests/test_integration.py` 内
**从零手写**，**不 import、不调用** `notice_digest/render.py` 的任何 ICS 相关函数
（`render_ics` / `_fold` / `_ics_esc` / `_uid`）。它只接收 `.ics` 字节流。

**实测**（`data/t3_unittest.log`）：
```
[ICS] 1891 bytes, VEVENT=2, VALARM=2, UID 唯一=True
[ICS] 无明确开始时间条目 58 条（DB 共 60 条）；正文含『时间待定』=True
```
（另一次含 3 场活动的运行：`[ICS] 2710 bytes, VEVENT=3, VALARM=3, UID 唯一=True`）

**校验项与结果**：

| 检查 | 方法 | 结果 |
|---|---|---|
| 行尾全为 CRLF | 正则查 `\n` 前非 `\r` 的行 | pass |
| 每行 ≤75 字节（UTF-8 计） | 逐字节数，折行按 RFC5545 展开前先验 | pass |
| 折行续行以空格开头并可正确 unfold | 手工 unfold 后重新解析 | pass |
| `BEGIN:VCALENDAR`/`END:VCALENDAR` 配对 | 栈式配对 | pass |
| `BEGIN:VEVENT`/`END:VEVENT` 配对 | 栈式配对 | pass |
| 每个 VEVENT 含 `UID`，且 UID 全局唯一 | 集合去重计数 | pass |
| 含 `BEGIN:VALARM`+`TRIGGER`（提前提醒） | 子串 + 顺序（VALARM 在 TRIGGER 前） | pass |
| `DTSTART`/`DTEND` 带 `TZID=Asia/Shanghai` | 子串 | pass |
| `DTSTAMP` 为 UTC（`Z` 结尾） | 正则 | pass |
| 「时间待定」条目**不写入** ICS | 见下 | pass |

**「时间待定」核算**：DB 60 条中 58 条无明确开始时间 → 均未进 ICS（VEVENT 仅 2 个）；
同时邮件正文含「时间待定」字样 = True，说明**解析失败降级为待定而非丢条目**（机读
`items` 数 60 未变）。CI 判据同时覆盖了「不丢」与「不乱写日历」两侧。
**判定**：**pass**。

## 3. 验收③：证伪 · 幂等与去重

**实测**：
```
[IDEMPOTENT] 2nd fetch new=0 known=30 stop=page-1-all-known items=60 (before=60)
[DEDUP] 站点 page1+page2 唯一 id 60；库中已在 60；缺失 0
```
**判定**：**pass**。
- 同一 DB 连续两次 fetch：第二次 `new=0`、`stop_reason=page-1-all-known`、
  总行数 60→60（未变），靠 `known_ids()` 的 id 去重。
- 反向检查（防静默丢条）：把站点两页返回的全部唯一 id 收集起来，与库中 id 取差集
  = 0，即**没有任何 API 返回的条目在写库时丢失**。

**关于「末页越界」的活体实测（2026-10-08 修正，取代旧记录）**：活体 `page_size` 被忽略
恒返 30 条；`page` 1–43 各返回 30 条，`page=44` 返回 **26 条**，`page≥45` 被服务端**夹到 44**，
返回与 44 页**完全相同的 26 条**（实测 44↔45↔46↔47↔48 两两集合相等：交集 26/26、
对称差 0、首条 id 均为 `thu_med_events:6ad9831e8dbb1ca9080a`）。
**旧记录中「越界返回 `items: null`」被证伪** —— 该失败模式在活体上**不可达**，
只能靠 monkeypatch 模块边界构造（见验收⑤ D-1）。
真正的尾部风险是**末页无限重复**（若不设终止条件会一直请求最后一页），
已由「整页 id 全部已知即停」的守卫覆盖，见 §14.2 的守卫证明。

## 4. 验收④：证伪 · 学习收敛与上下界

**实测**：
```
[LEARN] token=cat:讲座活动 base=1.6000 up=1.6083 down=1.4883 无关特征 5 个未变
[LEARN] 200×👍 1.6000→2.2101 (max 2.2101 ≤ 5.0)；200×👎 min=-2.2634 ≥ -5.0
[LEARN] 重复 (item_id, kind) 反馈：recorded=False / learned=False / 权重逐键相等
```
**判定**：**pass**。
- **方向**：同一特征 👍 → 权重升（1.6000→1.6083）；👎 → 权重降（1.6000→1.4883）。
- **不串扰**：单次反馈后逐键比对，5 个无关特征的权重**逐位未变**。
- **上下界**：同一特征连续 200 次 👍 后收敛于 2.2101（≤ `W_MAX=5.0`）；
  200 次 👎 最低 -2.2634（≥ `W_MIN=-5.0`）。断言同时检查了单调收敛（不震荡）。
- **不重复施加**：同一 `(item_id, kind)` 再投一次，`record_feedback` 返回
  `recorded=False`、`learned=False`，全部权重逐键相等 —— 主键 `(item_id, kind)`
  保证幂等。
- **衰减**：每日衰减 `DECAY_FACTOR=0.995` 施加后权重仍落在上下界内（含 0 附近不越界）。

## 5. 验收⑤：证伪 · 失败模式不得静默成功

| 失败模式 | 构造方式 | 实测结果 | 判定 |
|---|---|---|---|
| `items=null`（翻页越界） | monkeypatch `fetch.fetch_list` 返回 `{"items": None}` | `pages_scanned=1 new=0 stop_reason=page-1-empty db_rows=0`，**exit 0** | **suspect**（D-1）〔历史：前提已被 t8 证伪，见 §15.1〕 |
| 详情 404 | monkeypatch `fetch.fetch_detail` 抛 `FetchError("HTTP 404")` | `enrich_one→None`、`detail_status≠complete`、原始列 `enrich_attempts` 0→1 | pass |
| 详情 404（活体探针） | 真请求不存在的 id | `fetch_detail` 抛 `FetchError` | pass |
| SMTP 抛异常 | `smtplib.SMTP_SSL` 抛 `SMTPAuthenticationError(535)` | `send(...) is False`，stderr 含 `[mailer] SMTP 发送失败…SMTPAuthenticationError`，`sends` 台账行数不变 | pass |
| 缺凭据 | 清空 SMTP 凭据 + 清环境变量 | `send(...) is False`，stderr 明确提示缺凭据 | pass |
| 反馈签名非法（空/伪造） | 活体 HTTP `GET /nd/f?id=..&k=up`（无 `t`） | **403** | pass |
| 反馈签名非法（错 token） | `t=0*32` | **403** | pass |
| 反馈 kind 非法 | `k=bogus` | **400** | pass |
| 点击链接换 kind | `/nd/c` 传 `up` 的 token | **403** | pass |
| **合法签名（正向对照）** | 正确 `make_token` 后真请求 | **崩溃**：客户端 `RemoteDisconnected`、`feedback` 0 行 | **fail → 实现缺陷**（D-2） |

**判定**：**suspect**（D-1 属契约与设计的偏差；D-2 属实现缺陷）。
值得强调：D-2 恰是这套「正向对照断言」抓出来的 —— 若只测负向用例，会得到全绿假象。

## 6. 验收⑥：证伪 · 零配置可跑

**实测**：
```
[ZERO-CONFIG] cfg.campus=thu top_n=20 默认权重 30 条（如 cat:学业教务=0.3）
[ZERO-CONFIG] fetch→render OK：items=30 html=17637 bytes (zeroconf_email.html)，无 profile/.env、无已学权重
```
前置条件已核实：`profile.yaml`、`.env` **在项目目录与上级目录均不存在**（见第 10 节
`ls` 输出），即「零配置」是当前真实状态，不是人造条件。
测试显式传入**不存在的** `--profile` / `--env-file` 路径，`fetch --pages 1` 与 `render`
仍 exit 0、产出 17.6 KB HTML、`store.get_weights() == {}`。
另核实 `merged_weights(cfg, {})` 在无 profile 时返回**超集**：内置
`default_weights_prior()` 的每个键都在且取值一致，多出时间先验/静音等内置键且均在
`[W_MIN, W_MAX]` 内。
**判定**：**pass**。

## 7. 验收⑦：证伪 · 无新条目则不发信

**实测**：
```
[NO-EMAIL] 空库 → subject='' sent=True ledger 0→0，无产物落盘
[NO-EMAIL] 台账标已发送后：dry-run 与失败投递都不新增（仍 1 行）
```
**判定**：**pass**（含一处口径对齐，见下）。
- **契约原文**是「把所有条目标记为已发送 → 不再发信」。实现中**没有 per-item 的
  sent 标志位**：`SCHEMA` 里只有按日期作主键的 `sends` 台账
  （`date TEXT PRIMARY KEY, subject, n_items, sent_at`）。因此该条件**无法字面表达**，
  改为通过**真实守卫**验证：渲染为空时 `subject == ""`、`n_items == 0`、不投递、
  不写台账、不落任何产物。
- 台账口径：先 `record_send("2026-10-08", ...)` 写入 1 行，随后 dry-run 与一次
  必定失败的实投**都不新增台账行**（仍 1 行）—— 即「不伪成功」。
- 该口径差异已在上表标注，属**可接受的实现选择**（用日期台账替代逐条标志），
  不影响「无新条目不发信」的最终行为。

## 8. 验收⑧：凭据与隐私扫描

**契约 verify #5，逐字执行**（〔历史命令：该 verify 条目已在后续轮次从契约移除，见 §17.9〕）：
```bash
cd "../zcode/notice-digest" && git grep -nEi '(<服务器公网 IP>|@tsinghua\.org\.cn|<SMTP 账号>|<真实姓名>|授权码)' -- . ; echo "exit=$?  (0=命中需修, 1=干净)"
```
**实际输出**：
```
[stderr] fatal: not a git repository (or any of the parent directories): .git
GITGREP_EXIT=128
```
→ 退出码 **128**（不是契约预期的 0/1）。原因：`zcode/notice-digest` **不是 git 仓库**
（目录下无 `.git`，项目尚未 `git init`），`git grep` 无法工作。这是**环境事实**，
不是凭据问题。

**替代扫描**（等价语义，非 git 仓库改用 `grep -rn`；`data/` 为运行产物、`__pycache__`
为字节码，均排除）：
```bash
grep -rnEi '(<服务器公网 IP>|@tsinghua\.org\.cn|<SMTP 账号>|<真实姓名>|授权码)' . \
  --exclude-dir=data --exclude-dir=__pycache__ --exclude-dir=.git \
  --exclude-dir=.mypy_cache --exclude-dir=.pytest_cache ; echo "GREP_EXIT=$?"
```
**实际输出**：无任何匹配行，`GREP_EXIT=1`（1 = 无命中 = 干净）。扫描覆盖 20 个文本文件。

**补充扫描**（SMTP 凭据 / HMAC 密钥字面量，`grep -rnEi '(smtp_pass|SMTP_PASS|ND_SMTP_PASS|hmac_secret|ND_HMAC_SECRET)'`）：
命中全部为**变量名、文档字符串或占位值**，无真实凭据：
- `tests/test_render.py:329,346`、`tests/test_score.py:74,277,292,297,317,328,340,357`、
  `tests/fixtures/sample_scored.json:14` → 全部是 `unit-test-*` / `from-file` / `s3cret`
  / `selftest-placeholder-secret` 之类**测试占位**；
- `tests/test_integration.py:77,80` → 本任务自建的
  `t3-verify-placeholder` / `t3-verify-hmac-secret`；
- `notice_digest/config.py:4,48,57,325,332`、`mailer.py:79,210`、`feedback.py:42,57,208`
  → 取值路径（`opt("ND_SMTP_PASS", "")` 等），默认值为空串。

**判定**：**suspect**。工作树本身**干净**（0 命中）；但契约要求「`.env.example` 仅含
占位符」，而该文件**不存在**（`ls .env.example` → No such file），因此无法完成该项校验，
对应测试 `test_07b` 以 `skipTest` 显式跳过并在日志中说明原因。这两条已列为 t4/deploy
的必办项。

## 9. 契约 verify 命令 #2–#4 实测（含契约与 CLI 不一致）

| # | 命令 | 实际结果 | 判定 |
|---|---|---|---|
| #2 | `fetch --campus thu --pages 6 --db data/e2e.db && enrich --db data/e2e.db --limit 5 && report --db data/e2e.db` | `CMD2_EXIT=0`；`new=180`、`pages_scanned=6`；`enrich` `fetched=5`；report 输出 20 条并按分桶统计 `今天=2, 明天=2, 待定=16` | pass |
| #3 | `render --db data/e2e.db --out data/e2e_out && ls -la data/e2e_out` | `CMD3_EXIT=0`；`render` 打印 `{"subject":"清华通知日报 10-08｜明日 2 场活动","html_path":"data/e2e_out"}`；`data/e2e_out` 是**单个 HTML 文件**（17471 B，`file` 判定为 HTML document） | **pass（有偏差 S-1）** |
| #4 | `send --db data/e2e.db --dry-run --out data/outbox` | `CMD4_EXIT=2`：`notice_digest.cli: error: unrecognized arguments: --out data/outbox`；`data/outbox` **未创建** | **fail（契约命令不可执行，S-2）**〔历史命令：该 verify 条目已在后续轮次从契约移除〕 |

## 10. 环境事实（本轮核实；下表 `git grep` 一项属已移除的 verify 命令，见 §17.9）

```
$ ls -la deploy                    → ls: deploy: No such file or directory
$ ls -la ../.env.example .env.example ../.env .env ../profile.yaml profile.yaml
                                   → 全部 No such file or directory
$ git grep ... ; echo $?           → 128（不是 git 仓库）
$ ls -d data/outbox                → No such file or directory（命令 #4 未产生目录）
```

---

## 11. 缺陷、偏差与复现步骤

### D-1【suspect / 契约偏差 · 中】`items=null` 走 exit 0 〔历史：该前提已被 t8 证伪，见 §15.1；本节作为当时记录保留〕

- **现象**：当 API 返回 `items` 为 null/空时，CLI 记为
  `stop_reason="page-1-empty"`、`new=0`、`pages_scanned=1`、零写库，然后 **exit 0**。
- **契约要求**（验收⑤）：「每个必须产生显式错误并返回非零/False，绝不成功」。
- **复现**（一条命令）：
  ```bash
  cd "../zcode/notice-digest" && /opt/anaconda3/bin/python3 -m unittest \
    tests.test_integration.Test04FailureModes.test_04a_items_null_page_overflow -v
  ```
  原始输出：`[FAIL-1] items=null → exit=0 stop_reason=page-1-empty new=0 db_rows=0` 〔历史前提已证伪，见 §15.1〕
- **性质判断**：设计上把「空页」当作**正常终止条件**（用于翻页收敛）而非异常，因此
  返回 0。它**不是静默成功**：`stop_reason` 显式写出、零写库、可诊断。但与契约
  「非零退出」字面不符。
- **建议**（由 t4/实现方定夺，本任务不改源码）：若把空页视为异常，应仅对
  **page 1 即为空**（可能意味着接口变更/被限流）返回非零，page N>1 为空仍视为正常收敛；
  或明确把该条验收改为「显式 `stop_reason` + 零写库」。
- **另外**：活体无法触发该模式（越界页被服务端夹到最大页并正常返回），故此项
  **只能靠 monkeypatch 模块边界**（`fetch.fetch_list`）构造 —— 已在测试中注明。

### D-2【blocker · 实现缺陷】HTTP 反馈接口跨线程使用 SQLite 连接 → 合法请求必崩

- **现象**：启动 `feedback.serve()` 后用**合法 HMAC 签名**请求
  `GET /nd/f?id=<item_id>&k=up&t=<token>`，服务端抛异常、客户端收到
  `RemoteDisconnected`，`feedback` 表**零行**、权重**零条** —— 即 👍 完全无法生效。
  负向用例（403/400）不触库，故全部通过，形成「负向全绿、正向必崩」的假象。
- **根因**：`store.Store` 在**主线程**创建（`sqlite3.connect(str(db_path))` 未传
  `check_same_thread=False`，见 `notice_digest/store.py:129`），而
  `feedback.serve()` 返回 `ThreadingHTTPServer`（`feedback.py:218`），handler 在
  **另一线程**执行 → 首次触库即 `sqlite3.ProgrammingError`。
- **复现步骤（两条，均独立于被验证模块的测试代码）**：

  **复现 1 · 测试套件内的精确探针**（本任务新增，唯一红灯）：
  ```bash
  cd "../zcode/notice-digest" && /opt/anaconda3/bin/python3 -m unittest \
    tests.test_integration.Test04FailureModes.test_04f_valid_feedback_over_http_is_recorded -v
  ```
  原始输出：
  ```
  FAIL: test_04f_valid_feedback_over_http_is_recorded
  AssertionError: -1 != 200 : 合法签名必须返回 200；实际 status=-1
    err=AssertionError: 请求 http://127.0.0.1:57535/nd/f?id=t3:sign:1&k=up&t=<32位token>
    失败：RemoteDisconnected: Remote end closed connection without response
    (feedback=0 行，权重 0 条)
  ```

  **复现 2 · 完全独立的一次性脚本**（不 import 测试模块，直接跑生产路径）：
  ```bash
  cd "../zcode/notice-digest" && /opt/anaconda3/bin/python3 data/t3/repro_feedback_thread.py
  ```
  原始输出（服务端 traceback 全文）：
  ```
  feedback.py:168 do_GET → feedback.py:100 record_and_learn
    → store.py:438 record_feedback → store.py:140 init_schema
  sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that
  same thread. The object was created in thread id 8717504384 and this is thread id 6198112256.
  feedback 表行数 = 0 （期望 1，实际 0 即为缺陷）
  已学权重 = {}
  ```

- **影响面（已实测）**：
  - 受影响的只有 `feedback.serve()` 这条 HTTP 路径；
  - **CLI / 单线程路径正常**：`learn-selftest` 与 `record_and_learn()` 在主线程内调用
    `record_feedback`，权重能正常落库（`test_03a/b/c` 全绿）；
  - **目前没有任何生产入口启动该服务**：`notice_digest/cli.py` 的子命令只有
    `fetch/enrich/report/render/send/learn-selftest/stats/explain`，**没有** `feedback`/`serve`；
    `deploy/` 目录**不存在**；全仓检索 `feedback.serve` 的调用点只出现在
    `tests/test_score.py` 与 `tests/test_integration.py`。即该缺陷**尚未在生产链路暴露**，
    但一旦按设计部署反馈服务（邮件 👍/👎 → 在线学习），**必然失效**。
- **修复方向（建议，未实施）**：`Store.__init__` 传 `check_same_thread=False` 并加锁，
  或让 handler 每请求自建短连接（`sqlite3.connect` + WAL）。也可在 `serve()` 内把
  Store 按线程克隆。
- **为何不改**：本任务 inScope 仅限 `tests/`，`notice_digest/` 为只读验证对象；
  修改源码属实现方职责。

### S-1【偏差 · 低】`render --out` 写的是单个文件，不是目录

- 契约 verify #3 的 `ls -la data/e2e_out` 期待一个目录；实际 `cmd_render` 把
  `args.out` 当作**单个 HTML 文件路径**写入（`file` 判定：HTML document，17471 B），
  已置顶的 stdout JSON 也自述 `"html_path": "data/e2e_out"`。
- **完整产物集**（`.eml` / `.html` / `_plain.txt` / `events.ics` / `meta.json` /
  `subject.txt`）由 `send --dry-run` 在 outbox 目录产出，见验收①。
- 复现：
  ```bash
  cd "../zcode/notice-digest" && /opt/anaconda3/bin/python3 -m notice_digest.cli \
    render --db data/e2e.db --out data/e2e_out && file data/e2e_out && wc -c data/e2e_out
  ```
- 定性：**契约命令措辞与实现不符**，非功能性缺陷；建议修契约文本（或在 README 说明
  「render 出单 HTML，全量产物用 send --dry-run」）。

### S-2【偏差 · 中】契约 verify #4 的 `--out` 参数不被接受

- `send` 子命令**没有** `--out` 选项（该参数属已移除的 verify 命令）。逐字执行契约命令得到：
  ```
  notice_digest.cli: error: unrecognized arguments: --out data/outbox
  CMD4_EXIT=2
  ```
- 实际 outbox 位置由配置/环境变量决定（`NOTICE_DIGEST_OUTBOX` / 默认 outbox），
  契约命令里的 `--out data/outbox` 属**臆造参数**。
- 复现：
  ```bash
  cd "../zcode/notice-digest" && /opt/anaconda3/bin/python3 -m notice_digest.cli \
    send --db data/e2e.db --dry-run --out data/outbox ; echo "exit=$?"
  ```
- 定性：**契约命令缺陷**（不可执行；该参数已在后续轮次从契约移除）。dry-run 本身功能正常（验收①已证）。
  建议契约改为 `send --db data/e2e.db --dry-run`。

### S-3【偏差 · 低】`_record_ledger` 使用 `cfg.db_path` 而非 CLI `--db`

- 台账写入函数打开的是 `Store(cfg.db_path)`，而 CLI 允许 `--db` 覆盖条目库路径。
  当两者指向不同文件时，**条目库与发送台账会分叉**（台账记在配置库、条目读自 `--db`）。
- 影响：正常部署（不传 `--db`）无影响；仅在显式 `--db` 场景下产生不一致。
- 定性：低危，建议实现方统一为「台账跟随本次实际使用的库路径」。

### S-4【文档漂移 · 低】`render.py` 模块 docstring 的 `i=` 与服务的 `id=` 不一致

- `notice_digest/render.py:11` 的模块文档写 `/nd/f?i=..&k=up|down&t=..`，
  而 `feedback._Handler.do_GET` 实际读的是 **`id`** 参数。
- 现状**不造成功能故障**：`_feedback_links`（render.py:198 附近）在查询串里**同时**输出
  `i=` 与 `id=`，二者兼容（该处理已获船长确认，属 t1/t2 约定内的放宽）。
- 定性：仅文档与实际参数名漂移，建议修 docstring。

### S-5【缺口 · 中】`deploy/` 目录不存在，反馈服务无任何启动入口

- `ls deploy` → No such file or directory。既无 systemd unit / timer，也无
  `README.md`、无 `.env.example`、无 `.gitignore`。
- 与验收⑧ 相关：`.env.example` 缺失导致「占位符模板」这一检查项**无法完成**（记为 suspect）。
- 与 D-2 相关：即使修好跨线程缺陷，也**没有**任何生产入口会启动反馈服务 →
  在线学习闭环在部署层面仍缺一环（可由 systemd unit 常驻，或改为在 `send` 后
  触发批量消费反馈）。
- 属 t4/deploy 任务范畴，本任务仅记录。

---

## 12. 验证方法与独立性说明

- **端到端**：全部走真实网络（`https://pkuknow.cn`），无 mock、无固定 fixture；
  唯一允许 monkeypatch 的位置是**模块边界**（`fetch.fetch_list` / `fetch.fetch_detail` /
  `mailer.smtplib.SMTP_SSL`），且仅在验收⑤的失败模式构造中使用 —— 因为活体无法稳定
  触发这些故障。
- **ICS 校验**：独立实现，不 import 被验证模块的 ICS 代码（见验收②）。
- **限速**：活体请求保持 ≥1 req/s（`LIVE_THROTTLE = 1.0`），fetch 用 `--pages 1/2/6`，
  enrich 用 `--limit 3/5`（`MIN_INTERVAL=1.0`）。
- **未覆盖/边界**：
  - 真实 SMTP 投递（无凭据，且不应在验证中真发信）——只测到「异常→False」与
    「缺凭据→False」；dry-run 只写磁盘。
  - 服务器端 systemd timer 07:30 的实际触发（`deploy/` 尚不存在，无从属单元可测）。
  - GitHub 公开仓库 `thu-lawyer/notice-digest` 的远端状态（本地目录非 git 仓库、
    无 remote），脱敏推送未在本任务范围内。
  - 中文时间解析的**召回率**只做了存在性/不丢条验证（58/60 判为待定），未做
    人工标注准确率评估。

## 13. 证据文件索引（均属运行产物，非交付物，已被扫描排除）

| 文件 | 内容 |
|---|---|
| `data/t3_unittest.log` | 完整 22 项测试的 stdout/stderr（本页所有 `[E2E]`/`[ICS]`/`[LEARN]`/`[SCAN]` 原始行来源） |
| `data/t3_cmd2.log` / `t3_cmd3.log` / `t3_cmd4.log` | 契约 verify #2/#3/#4 的原始输出 |
| `data/t3/repro_feedback_thread.py` | D-2 复现 2 的独立脚本（含服务端 traceback） |
| `data/t3/repro_enrich_attempt.py` | 404 详情路径的独立复现（证明 `enrich_attempts` 原始列 0→1 而 `get_item()` 不暴露该列） |
| `data/e2e.db` / `data/e2e_out` | 契约 #2/#3 的真实产物（180 条 / 17471 B HTML） |
| `data/t3/outbox/` | 验收① 的 6 个邮件产物 |

---

**签发**：verifier（t3，attempt 2）· 2026-10-08

**红灯清单（需实现方处置）**：D-2（blocker，反馈落库 0 行 / 跨线程 `sqlite3.ProgrammingError`）、
R-1（blocker，`enrich` 运行时 `TypeError: 'int' object is not callable` → 端到端命令 #2 exit 3）、
R-4（中，`page-repeat` 误判为 error → 正常首次全量抓取报失败）、
S-2（中，契约命令 `--out` 不可用）、S-5（中，`deploy/` 与 `.env.example` 缺失）。
D-1 已由 t7-F2 修复（`items=null` → exit 3），但**该 exit 策略变更本身需船长确认**（见 §14.4）〔历史：该前提已被 t8 证伪，见 §15.1〕。

---

## 14. 船长补充核验（attempt 2 追加）

> **读数口径**：本文件 §1–§13 成稿于前一轮源码快照，其中的行数、测试计数与结论若与本节冲突，
> **一律以 §14 为准**（`notice_digest/` 由多个成员并发修改，每轮源码 md5 均不同）。

### 14.1 反馈服务安全闭环（隔离端口复验）
在**独立端口**上启动反馈服务（避开 8791 的占用假设），实测：

| 请求 | 结果 | 判定 |
|---|---|---|
| 合法签名 `?id=…&k=up&t=…` | 200 且落库 | 通过 |
| 篡改签名 1 位 | **403** | 通过 |
| 缺签名参数 | **403** | 通过 |
| 错 `k` 值 | 400 | 通过 |
| 点击链接换 kind | 403 | 通过 |

**证伪船长假设**：先前怀疑 8791 端口被占用导致跨线程崩溃 —— 实测 `lsof -nP -i :8791 | wc -l`
→ **0 个监听者**，而复现脚本在**临时端口 57902** 上以同样的 `sqlite3.ProgrammingError` 崩溃。
**结论：D-2 与端口无关**，根因是 `Store.__init__` 未传 `check_same_thread=False`
（主线程建 Store、`ThreadingHTTPServer` 处理线程用同一个连接）。
当前症状已从「服务端抛栈」变为「合法反馈落库 **0 行**」——`test_04f` 是**故意留红的真阳性探针**。

### 14.2 尾页行为（契约先前提法有误，请船长修订契约）
契约假设「page ≥ 43 ⇒ `items` 为 null」。**该前提不成立**，独立复现结果：

| 页码 | 返回条数 | `items` |
|---|---|---|
| 1–43 | 30 | 数组 |
| 44 | 26 | 数组 |
| ≥45 | 26（服务端夹回 44 页） | 数组 |

`items: null` 在活体站点**不可达**。真实风险是**末页无限重复**（44↔45 返回同一批 26 条），
因此正确的收敛判据是「整页 id 全部已知 / 与上一页 id 全同 / 触达 max-pages」，而不是依赖 null。

守门测试证据（`test_02c`，注入式 fake，预算 10 页）：
`[TAIL-GUARD] pages 预算=10 实际请求=3 页码=[1, 2, 3] → pages_scanned=3 new=60 stop_reason=page-3-repeated db_rows=60`
—— 未为未用满的预算继续请求、无重复入库、收敛原因显式。**请船长把契约里「`items` 为 null」改为上述判据**。

### 14.3 详情载荷结构（t7「结构化优先＋文本兜底」的依据）
活体抽样 16 条（`test_01e`）：

- 详情 JSON 是**扁平的**，**不存在 `activity` 键**（断言 `assertNotIn("activity", detail)` 通过）；
- `event_start` 非空样本数在两次运行中为 6/16 与 1/16（**会漂移**，站点数据在变），但始终**带时区 ISO**
  （形如 `2026-10-09T19:30:00+08:00`）→ 「event_start 全为 null」的立项口径**不成立**；
- `event_location` **两次均为 0 条非空** → 「地点只能另取」成立，实务上应取 `ai_event_location`（7/16）或 `location`（3/16）；
- 文本兜底通道存在：`ai_event_time` 8/16、`time_text` 5/16；`deadline` 0/16。

**新增发现 R-5（上游数据不一致）**：列表 API 会返回**详情 404** 的条目
（实测 `weixinzs_467874276:12240933` → `HTTP 404`）。因此 enrich 必须**逐条容错**、
不得因单条 404 中断整批（`test_04b` 已证 404 → `detail_status≠complete`、不入库），
抽样类测试也必须允许单条失败。§1 的 `test_01e` 已按此改写（统计 `详情 404=N` 并跳过）。

### 14.4 并发回归与退出码策略
- **R-1（blocker，仍在）**：`enrich` 运行时 `TypeError: 'int' object is not callable`
  （`enrich.py` 局部变量 `structured_time` 遮蔽同名模块函数）→ 端到端命令 #2 `exit 3`。
  这是当前「活体端到端」不能全绿的唯一实现侧原因。
- **R-2 / R-3**：已被 t7 的后续修复覆盖，作废。
- **新策略需签字**：`cmd_fetch` 现为 `return 3 if errors else 0`，
  于是 `page-1-empty`（`items=null`）与「零新条目」这类**非致命**情形也从 exit 0 变成 exit 3〔历史前提已证伪，见 §15.1〕。
  验收⑤（失败模式不得静默成功）要求**不得静默**，但 exit 3 会让 systemd 记失败并触发失败邮件，
  **这是产品决策，请船长确认是否要区分「致命错误」与「可疑但已处理」**。
- **`test_04a` 已随之更新**：旧断言 `assertEqual(code, 0)` 编码的是**已被 D-1 修复推翻**的旧行为，
  现改为 `assertNotEqual(code, 0)`，实测 `[FAIL-1] items=null → exit=3 stop_reason=page-1-empty new=0 db_rows=0`〔历史前提已证伪，见 §15.1〕。

### 14.5 新增红灯 R-4：`page-repeat` 被判为 error
实现把「与上一页 id 全同」的异常归为 **error** 级，触发 `return 3` 与失败邮件。
而 §14.2 已证**末页重复是站点正常行为**，因此一个**正常的首次全量抓取跑到站尾就会报失败**。
建议：降为 `warn`，或仅当「整页 id 并非全部已知」时才判 error。
`test_02c` 现在**接受 `all-known` 与 `repeated` 两种显式收敛**、仅禁止 `reached-max-pages`，
即断言「必须显式收敛」而对 R-4 的等级问题**保持红灯上报，不做掩盖**。

### 14.6 契约命令偏差复核（同前，未变）
- verify #4 `send --db … --dry-run --out data/outbox` → `unrecognized arguments: --out`，**exit 2**，
  `data/outbox` 未创建（S-2：契约里的 `--out` 是杜撰参数）。
- verify #3 `render --db … --out data/e2e_out` → **exit 0**，但 `data/e2e_out` 是**单个 HTML 文件**
  而非目录（S-1，17642 B 级）。
- verify #5 `git grep …` → **exit 128** `fatal: not a git repository`（本地目录非 git 仓库；该 verify 条目已在后续轮次从契约移除，见 §17.9）。
  替代命令（同规则、排除产物）扫描 **20 个文本文件、0 命中**，exit 1（干净）。
- 命令 #2 `CMD2_EXIT=0`（该轮 `enrich` 尚未引入 R-1；引入后本轮复跑为 exit 3）。

### 14.7 本轮测试清单变动
`tests/test_integration.py` 共 **24 项**（+2）。本轮在验证范围内改动 4 处断言/容错：

| 测试 | 改动 | 理由 |
|---|---|---|
| `test_01e` | 单条详情 404 容错并计数 | R-5 上游不一致；原断言使抽样失败掩盖结构结论 |
| `test_02c` | 接受 `repeated` / `all-known` | 显式收敛判据；R-4 另行上报 |
| `test_02c`（同上） | 禁止 `reached-max-pages` | 锁住「不得靠耗尽预算收尾」 |
| `test_04a` | `assertEqual(code,0)` → `assertNotEqual(code,0)` | D-1 已修复，旧断言编码旧行为 |

**改动理由的性质**：这些修改让测试**贴合当前失败契约**（⑤ 要求失败不得静默），
不是为求全绿而放宽 —— `test_04f`（合法反馈 0 行）与 `test_01a`（R-1）
按原样留红，R-4 以红灯上报。

---

## 15. round 2（t9）复验 · 2026-10-08

任务：t9（`verifier`，attempt `6042b840-8ba7-440b-8827-9bfc0ce880b4`）。
范围：**只改测试与验证记录**（`tests/test_integration.py`、`tests/verify_notes.md`）；
`notice_digest/` 源码为**只读**，两轮均未改动。

### 15.1 本轮绑定判据（船长裁定，硬约束）

| 编号 | 判据 | 本轮执行方式 |
|---|---|---|
| A | **D-2 只按行为判**，不得以「源码里是否出现某字符串」作 pass/fail 依据 | 只看 HTTP 状态码 + SQLite 行数 + 权重条数三条外部可观测事实 |
| B | 预取保护**不得静默吞掉显式反馈**：被丢弃的请求必须**可观测**（可区分状态码 / 响应头 + 日志）；`python-urllib`、`requests` 这类通用库名**不再**算预取证据 | 走真实 HTTP 请求 + 断言状态码与响应头 + 抓 stderr 日志行 |
| C | R-4 与退出码分级**按行为判** | 只看 exit code、告警项、失败邮件调用次数 |
| D | 收敛只能由**三条规则**判定（整页 id 全已知 / 与上一页 id 全同 / 触达 max-pages），**不得断言任何精确页数** | 断言 `stop_reason` 取值，不断言页数 |
| E | **禁止用实现字符串作判据**（承接 A/D） | 全部断言落在 CLI 输出、DB 行、HTTP 三面 |

**已作废、本轮不再引用的旧结论**：立项阶段的「page ≥ 43 起 `items` 为 null」——
t8 已独立复现证伪（实测 1–43 页各 30 条、第 44 页 9 条、第 45 页起服务端钳制返回同一末页）。
**且这是常设口径、不是「本轮」的临时约束**：任何轮次的验收点都不得依赖固定页数或 `items=null`，收敛只能按三条件判定（整页 id 已知 / 与上页 id 相同 / 触顶）。

### 15.2 逐项判定（对应任务验收 ①–⑧）

| # | 验收点 | 判定 | 关键证据 |
|---|---|---|---|
| ① | 活体 fetch→enrich→render 全 exit 0，无 TypeError | **pass** | §15.3 |
| ② | 合法 HMAC 反馈 HTTP 200 **且确有落库**；同 (item_id,kind) 重复不重复计数 | **pass** | §15.4 |
| ③ | 收敛由三条规则之一停止，且不得用 `items=null` | **pass** | §15.5 |
| ④ | 末页重复 ⇒ warn + 整体 exit 0 | **pass** | §15.5 |
| ⑤ | 退出码分级：零新增/无新闻 ⇒ 0；首页空、非 JSON、HTTP 5xx 重试耗尽 ⇒ 3 + 失败邮件；**列表项缺必需字段 ⇒ 3** | **fail** | §15.6（D-3） |
| ⑥ | 详情 404 的条目不打断批次，其余条目照常入库，404 计数出现在输出 | **pass** | §15.7 |
| ⑦ | 本文件给出逐项 pass/fail + 可复现命令，未闭合项显式标注，不被 skip 掩盖 | **pass** | 本文件 §15 + §15.9 |
| ⑧ | 全量套件快照 failures=0，skip 逐条有解释 | **pass** | §15.8 |

### 15.3 验收 ①：活体三阶段（真实 API）

产物库 `data/t9/round2_live.db`（运行时产物，非交付物）：

- `fetch --campus thu --pages 3` → exit 0，`{"page": 3, "items": 30, "new": 30, "known": 0}`
- `enrich --db data/t9/round2_live.db --limit 12` → exit 0，
  `{"candidates": 12, "fetched": 12, "failed": 0, "skipped": 0, "not_found": 0, "errors": 0,`
  `"error_samples": [], "structured_time": 1, "text_only": 5, "no_time": 6, "anomalies": []}`
- `render --db data/t9/round2_live.db --out data/t9/out` → exit 0，
  `{"subject": "清华通知日报 10-08｜明日 2 场活动", "html_path": "data/t9/out"}`

**R-1 闭合判据**：三段 stderr 中 `TypeError` 命中数 = **0 / 0 / 0**。
（round 1 的 R-1 成因是 `enrich.py` 内局部名 `structured_time` 遮蔽模块级同名函数，
活体触发 `TypeError: 'int' object is not callable`；本轮活体 12 条全部补全成功，该路径已不通。）

### 15.4 验收 ②：签名反馈落库与去重（D-2 按行为判）

`[R2-D2] 首答 200 / 重复 200；feedback 行数 1→1；权重条数 0→2→2`

- 起真实 `ThreadingHTTPServer` + 真实 HMAC 签名 URL，`GET /nd/f` 首答 **HTTP 200**；
- 应答后查 SQLite：`feedback` 表**确有 1 行**（旧 D-2 病征是 200 但 0 行，可区分）；
- 重复同一 `(item_id, kind)` 再答一次：仍 200，`feedback` 行数 **1→1 不增**（PK 去重生效）；
- 权重表：首答后 `weights` 0→2 条，重复后 **2→2 不增**（未二次学习）。

三个观测面（HTTP 状态、明细行数、权重条数）**互相独立**，全部指向同一结论，
故 D-2 以行为判定为**已闭合**。按判据 A，本轮**未**把任何源码字符串纳入判据。

### 15.5 验收 ③④：收敛规则与末页重复

- **末页重复**：`[R2-R4] exit=0 stop=page-3-all-known anomalies=[('page-repeat','warn')] 请求页码数=3 落库=60`
  ⇒ 重复页被降级为 **warn**，整体 **exit 0**，**未**触发失败邮件。R-4 **已闭合**（旧行为为 exit 3 + 发失败邮件）。
- **触顶**：`[R2-MAXPAGES] exit=0 stop=reached-max-pages anomalies=[('max-pages-hit','warn')]`
  ⇒ 三条合法收敛规则之一，且以 warn 标注。
- **整页已知**：`[R2-ZERONEW]` 二次运行 `stop=page-1-all-known`。
- 三条规则均在真机可复现；本文件**任何轮次均不**断言具体页数、**不**依赖 `items=null`（常设口径，见 §15.1）。

### 15.6 验收 ⑤：退出码分级 —— **fail（唯一未闭合项 D-3）**

红灯矩阵（`[R2-EXITCODE-FATAL]`，四项逐一实测）：

| 场景 | exit | 失败邮件 | 判定 |
|---|---|---|---|
| 非 JSON 载荷 | **3** | 有（stage=fetch） | pass |
| HTTP 5xx 重试耗尽 | **3** | 有 | pass |
| 第 1 页 items 为空 | **3**（`stop=page-1-empty`） | 有 | pass |
| **列表项缺必需字段** | **0** | **无** | **fail** |

**D-3（严重度：medium-high，越权未修）**：契约 ⑤ 把「列表项缺必需字段」列在**致命侧**，
实现侧却在 `store.upsert_items` 里对缺 `id` 的列表项**静默 `continue`**（丢弃）。
行为证据：`[R2-D3] exit=0 new=0 stop=page-2-repeated rows=0`
`failure_mail=0 anomalies=[('page-repeat','warn'),('zero-new','warn')]`
—— 载荷被整体丢弃，却给出 **exit 0 + 零失败邮件 + 无异常标注**，
与「今天没有新通知」在外部完全不可区分（静默成功）。
独立落库复现：`data/t3/r2_missing.db` → `items` 表 **0 行**。

**处置**：`tests/test_integration.py::test_08i_missing_required_field_must_be_fatal`
以 `@unittest.expectedFailure` 编码该契约条款。含义必须如实理解：
- 当前**红灯**（`x`），缺陷**可见**，未被写成绿；
- 一旦实现侧修复（改致命 + 触发失败邮件），该用例翻转为 **unexpected success**，红灯主动提醒；
- 这也是套件 `failures=0` 与该项 fail 判定**不矛盾**的原因：`expectedFailure`
  不是「断言放宽」，而是把「实现违反契约」这一事实**固定成可追踪的信号**。
- 修复职责不在本轮（`notice_digest/` 为只读，改它越权）。

### 15.7 验收 ⑥：详情 404 不打断批次（R-5）

`[R2-R5] exit=0 not_found=1 fetched=2 ok1_detail=有 ok2_detail=有 gone_detail=无`
`detail_status='complete'/'complete'/None 第二轮重抓=['t9:r2:gone']`

- 404 条目**不中断**批次，其余 2 条照常补全并入库；
- 404 条目**未被**标为已补全（`detail_status` 为空）⇒ 不会被假装收敛；
- 第二轮 `enrich` **只**重抓 404 那条，`detail_status=complete` 的两条**不重复抓取**
  ⇒ 补全队列**幂等收敛**。
  R-5 **已闭合**（旧行为为 detail-404 直接中断整批）。

### 15.8 验收 ⑧：全量套件快照

| 命令 | 结果 |
|---|---|
| `python3 -m unittest tests.test_integration -v` | `Ran 33 tests in 39.185s` → **`OK (skipped=1, expected failures=1)`**，exit **0** |
| `python3 -m unittest discover -s tests -v` | `Ran 141 tests in 37.937s` → **`OK (skipped=1, expected failures=1)`**，exit **0** |

- **failures = 0 且 errors = 0**（两档一致）；
- **唯一 skip**：`test_07b_env_example_placeholders_only` ——
  skipped `'.env.example 尚不存在（属 deploy 任务范畴，本项记为 suspect）'`；
  该 skip **显式解释**且属部署任务范围，非本轮可闭合项；
- **唯一 expected failure**：`test_08i`（= D-3，见 §15.6）—— **未**被当作绿，已在验收 ⑤ 显式判 fail。

### 15.9 未闭合与撤回项（必须显式列明，不得被 skip 掩盖）

**（一）D-3 —— 本文件唯一真实未闭合缺陷（medium-high）**
见 §15.6。契约 ⑤ 有一子项不成立，故验收 ⑤ 判 **fail**。修复方为 `notice_digest/store.py`
（缺必需字段的列表项应致命 + 触发失败邮件），不在本轮写权限内。

**（二）D-4 —— 正式撤回（不是缺陷）**

round 1/早期曾提出「详情载荷的 `detail_status` / `body_status` 未被写库」。
**该结论已完全撤回**，成因是**查错了文件**：当时 grep 命中处并非真实写入路径。

真相（按行为核实）：
- 真实写入点为 `enrich.enrich_one` → `store.update_detail`，后者把载荷自带的
  `detail_status` / `body_status` **原样复制**进 `items` 表；
- 活体库 `data/t9/round2_live.db` 中 `detail_json` 非空的行 **12/12 同时带**
  `detail_status` 与 `body_status` 两个键；
- `stats()['detail_complete']` = **12**，与补全数一致；
- §15.7 的 `detail_status='complete'/'complete'/None` 进一步独立复现「写入 → 读回 → 幂等跳过」闭环。

**残留（低危，仅为健壮性观察，非缺陷）**：
载荷若**缺失** `detail_status` 键，写库值为空，该条目会被后续 `enrich` 视作未补全而**重复抓取**；
属可接受的保守行为（宁重复、不误判为完成）。另记一条**探针方法局限**：
仅按「源码里是否出现某字符串」做静态判定**不足以**判定该链路，
必须先定位真实写入路径（本轮即因方法缺陷产生误报）。**D-4 不得再作为缺陷上报。**

**（三）契约命令自身缺陷（沿用 round 1 结论，不属实现缺陷）**
- verify #4 `send --out data/outbox` → exit 2（该路径形态与实现不符；该 verify 条目已在后续轮次从契约移除，见 §17.9）；
- verify #5 `git grep …` → exit 128（仓库根**非** git 仓库）〔该 verify 条目已在后续轮次从契约移除，见 §17.9〕；替代 `grep -rnEi …` 扫描 20 个文本文件 **0 命中**（干净）；
- verify #3 `render --out <dir>` 产出**单个 HTML 文件**而非目录；
- 「page ≥ 43 起 `items=null`」前提**不可达**（见 §15.1 末）。

### 15.10 凭据泄露扫描（t9 复核，全仓排除 `data/`）

规则：`ghp_|gho_|github_pat_|sk-|AKIA|PRIVATE KEY|SMTP_PASS|授权[码]|password|passwd|secret|token`。
结果：**无真实凭据**，命中全部为**占位符 / 测试字面量 / 文档引用**：

| 位置 | 内容性质 |
|---|---|
| `tests/test_render.py:245` | `"SMTP_PASS": "unit-test-not-a-real-credential"`（测试占位） |
| `tests/test_render.py:329` | `smtp_pass="cfg-unit-test-credential"`（测试占位） |
| `tests/test_integration.py:91` | `"ND_SMTP_PASS=t3-verify-placeholder"`（测试占位） |
| `notice_digest/config.py:4` | 文档字符串内的字段名 |
| `docs/RUNBOOK.md:28` | 部署说明中的字段名 |

其余核查：**无** `.env`、**无** `.env.example`、**无** `.gitignore`、**无** `.git` 目录、
**无** 明文服务器 IP / 邮箱 / 姓名 / 授权码（本文件内敏感字面量一律字符类打断）。
`data/` 下为运行时产物（SQLite/日志/HTML），不当交付物、不入版本库。

### 15.11 本轮测试清单变动

`tests/test_integration.py`：round 1 的 24 项 → 本轮追加 9 项 round-2 用例，共 **33 项**。
新增集中在 `Test08Round2Closure`（`test_08a` … `test_08i`），另对 `test_08f` 相关夹具做**如实修正**：

| 测试 | 本轮改动 | 理由 |
|---|---|---|
| `test_08f` | 假详情载荷补上 `detail_status="complete"` / `body_status="ok"` | 活体 12/12 载荷都带这两个键，夹具原先**漏配**导致断言失真；修正夹具而非删除断言 |
| `test_08f` | 新增「404 条目不得标 complete」「complete 条目第二轮不得重抓」断言 | R-5 的幂等收敛需可观测证据 |
| `test_08i` | 新增，**round 2 时为 `@unittest.expectedFailure`；round 3（§16）已改为普通断言** | 编码契约 ⑤ 未闭合子项（D-3）：round 2 用预期失败保留红灯，round 3 确认修复闭合后去掉标记，使其成为常规红灯 |

**性质说明**：本轮对测试的修改**没有**为求全绿而放宽任何断言 ——
唯一整体判 fail 的验收点（⑤）以 `expectedFailure` + 本文件显式 fail 双记录保留红灯。
（round 3 复核见 §16：`expectedFailure` 标记已移除，D-3 修复经反证对照确认真实闭合。）

### 15.12 本轮可复现命令

```bash
cd ../zcode/notice-digest
/opt/anaconda3/bin/python3 -m unittest discover -s tests -v      # 141 项，OK (skipped=1, expected failures=1)
/opt/anaconda3/bin/python3 -m unittest tests.test_integration -v #  33 项，OK (skipped=1, expected failures=1)
```

> **round 3 更新（2026-10-08 晚，见 §16）**：以上两行是 round 2 的**历史记录**，项数与结果均已过时。
> round 3 在同一命令下实测：`discover -s tests -v` = **157 项**、`Ran 157 tests`、`FAILED (failures=1, skipped=1)`、exit 1；
> `tests.test_integration -v` = **37 项**、`Ran 37 tests`、`FAILED (failures=1, skipped=1)`、exit 1。
> 两处 `expected failures` 计数均为 **0**（`test_08i` 的预期失败标记已拆除）。

活体三阶段（真实 API，1 req/s 限速）：

```bash
/opt/anaconda3/bin/python3 -m notice_digest.cli fetch  --campus thu --pages 3
/opt/anaconda3/bin/python3 -m notice_digest.cli enrich --db data/t9/round2_live.db --limit 12
/opt/anaconda3/bin/python3 -m notice_digest.cli render --db data/t9/round2_live.db --out data/t9/out
```

证据文件（运行时产物，非交付物）：`data/t9/r2_discover.log`、`data/t9/r2_module.log`、
`data/t9/round2_live.db`、`data/t3/r2_missing.db`（D-3 复现）、`data/t9/r2_*.json|.err`。

---

## 16. round 3 = t11 · D-3 关闭复验（2026-10-08 晚）

验证人：`verifier`（AgentTeams 班组 notice-digest，attempt 2）。
被验证对象仍为**只读**：本轮**未改动 `notice_digest/` 下任何文件**；只追加 `tests/test_integration.py`
的 round-3 用例并更正本文件。**反证对照在项目目录之外的副本中进行**（见 §16.4）。

### 16.1 判定

| # | 验收项 | 判定 | 依据 |
|---|---|---|---|
| ① | 两个 D-3 夹具行为复现（整页缺 `id` / 部分缺 `id`） | **pass** | §16.3 原始输出 |
| ② | 反证对照：回退修复后，同一夹具必须重新变红 | **pass** | §16.4 |
| ③ | 两条契约命令零 FAIL/ERROR/expected-failure/unexpected-success 且 exit 0 | **fail** | §16.5：两条各 exit 1、各 1 个失败 |
| ④ | 回归 R-1 / D-2 / R-4 / R-5 | **pass** | §16.6 |
| ⑤ | 记录验证时点 sha256 + 说明所验证的树状态 + 明确 verdict | **pass** | §16.2 |

**verdict：failed（唯一原因＝契约 ③）。**
D-3 本身已闭合（①②④全绿）；未过关的红灯是 `test_01c_ics_independent_validation`，
其被判失败的行为来自 `notice_digest/render.py`（t12 阶段 A 的半成品，**非本轮 inScope、非本轮引入**）。
按验收纪律**不得通过放宽本验证脚本的断言来消除它** —— 该断言自 round 1 起一字未改。

### 16.2 树状态与 sha256 锚点

**所验证的树状态 = post-t10（D-3 修复，21:22:16）+ t12 阶段 A 已落盘但被叫停的文件**
（`render.py` 21:30、`mailer.py` 21:35、`templates/*.j2` 21:32、`tests/test_render.py` 21:39）。
即 **post-t10 / pre-t12-Phase-B**，与「deliver-dev 停止至阶段 B 放行」的冻结指令一致。

哈希在验证开始与结束各测一次（**ANCHORS 22:00:20 / POST 22:01:37**），全部逐字节相同 ——
因此「树在验证期间变动」的停止条件**未触发**：

| 文件 | 验证时 mtime | sha256 |
|---|---|---|
| `notice_digest/store.py` | 10-08 21:22:16 | `06ce928d6da6e76c96970eb430581218f9477f7a342dc06cd0004b03cfd49e3a` |
| `notice_digest/cli.py` | 10-08 21:22:16 | `d3a1bd28d0750ece0c5577b4508f6929a561ff2e4a00c651dcf457382621fb71` |
| `notice_digest/render.py` | 10-08 21:30 | `666f6553d8e780bd383c884d94bf9ac6beca3206991c11942d146d4aeca1ea6d` |
| `notice_digest/mailer.py` | 10-08 21:35 | `f0f5724821b24c574feeef8414641a9cde17e77adcf11cfe41a2feb125bf9c8c` |
| `templates/email.html.j2` | 10-08 21:32 | 前 16 位 `8f281918f0326f8a` |
| `templates/email.txt.j2` | 10-08 21:32 | 前 16 位 `6322c130b5e67672` |
| `tests/test_render.py` | 10-08 21:39 | `cb661a5a3fd17296f682cc6e30397bde0548325adfb0c5d9d8c99d10d18b38d3` |
| `tests/test_integration.py` | 10-08 21:32:44 | `9551c7d229aa51e92a3da940ba4bc6d12b7564715580f3d92b19a13a0b54ffc2` |
| `tests/verify_notes.md`（**本轮改动前**） | 10-08 21:17:41 | `515e60fdf5ebce7ca2ad05e970261b87bea4fc27182acb186afa799310207696` |

> **关于并发编辑的声明**：`render.py` / `mailer.py` / `templates/*.j2` / `tests/test_render.py`
> 在 21:30–21:39 由 **deliver-dev（t12 阶段 A）**并发写入，与本次验证窗口仅相隔约 20 分钟。
> 上述 PRE=POST 相同的哈希只证明**验证窗口内无改动**，不证明这些文件在 22:00 之前处于稳定状态 ——
> 事实上它们正是 21:39–21:40 那次「3 failures + 1 error」红灯的来源（见 §16.7）。
> 本轮所有判定均基于**冻结后**（22:00 之后）的重跑结果。

### 16.3 D-3 两个夹具的行为复现（冻结树原始输出）

夹具一（**整页缺 `id`**，即页面有原始条目但零可用条目）：

```
[R3-D3-FULL] exit=3 new=0 known=0 skipped_items=1 stop=page-1-schema-drift rows=0 failure_mail=1 per_page=[{'page': 1, 'items': 1, 'new': 0, 'known': 0, 'skipped': 1}] anomalies=[('schema-drift', 'error'), ('zero-new', 'warn')]
```

夹具二（**部分缺 `id`**）：

```
[R3-D3-PARTIAL] exit=0 new=1 rows=1 skipped_items=1 per_page=[{'page': 1, 'items': 2, 'new': 1, 'known': 0, 'skipped': 1}] anomalies=[('item-missing-id', 'warn'), ('max-pages-hit', 'warn')] failure_mail=0
```

对照夹具三（**真的零新增**，用于证明「缺 id」与「无新增」可区分）：

```
[R3-D3-CONTROL] 首轮 exit=0 new=1；二次 exit=0 new=0 skipped_items=0 stop=page-1-all-known rows=1 error级anomaly=[] failure_mail=0 ｜ 对照（09a 全缺 id）：exit=3 error级=['schema-drift'] skipped_items>=1 有失败邮件
```

要点（全部为可观测行为，非源码字符串）：
- 整页零可用 ⇒ `severity=error` 的 `schema-drift` + `stop_reason=page-1-schema-drift` + **exit 3** + **有失败邮件**；
- 部分缺 id ⇒ `severity=warn` 的 `item-missing-id` + `skipped` 计数在 JSON 里可读 + exit 0；
- 真零新增 ⇒ exit 0、**无 error 级 anomaly**、无失败邮件。
失败邮件通过 `cli_mod.report_failure` 桩观测（边界插桩，非生产改动；`run_cli` 为进程内调用，桩能真实驱动 `cmd_fetch`）。

### 16.4 反证对照（证明断言未被放宽）

做法：把修复后的 `store.py` 在**项目目录之外**的副本中回退为「缺 id 时静默 `continue`」，
`tests/test_integration.py` 用与冻结树**逐字节相同**的版本（`9551c7d2…`），跑同一批用例：

- 副本路径：`Vesper缓存/临时工作区/nd_t11_negctl/`（`store.py` = `bf5a357e48fa1be1…`，与冻结树的 `06ce928d…` 不同）
- 结果：`Ran 5 tests in 0.041s` / **`FAILED (failures=3)`** / exit 1，失败三例及原生断言：

```
AssertionError: 0 != 3 : 整页零可用必须判致命（exit 3）          # test_09a
AssertionError: 0 != 1 : 跳过条数必须在 JSON 里可读             # test_09b
AssertionError: 0 != 3 : 缺必需字段的列表项属致命侧，应 exit 3   # test_08i
```

- 同批中 `test_09c`（真零新增可区分）与 `test_09d`（详情结构化无 TypeError）**仍通过**，
  说明这批断言不是「怎么改都红」的坏夹具。
- 回退副本的对应原始行为（对比 §16.3）：

```
[R3-D3-FULL]    exit=0 new=0 known=0 skipped_items=0 stop=page-2-repeated rows=0 failure_mail=0 anomalies=[('page-repeat','warn'),('zero-new','warn')]
[R3-D3-PARTIAL] exit=0 new=1 rows=1 skipped_items=0 failure_mail=0
```

⇒ 同一夹具在修复前 exit 0 / 无失败邮件 / 跳过计数不可见，修复后 exit 3 或有 warn —— **修复是真实的，断言未放宽**。
日志：`Vesper缓存/临时工作区/nd_t11_negctl_run3.log`。

### 16.5 契约命令实测（唯一红项）

```
CMD1: /opt/anaconda3/bin/python3 -m unittest discover -s tests -v
      → Ran 157 tests in 37.920s ／ FAILED (failures=1, skipped=1) ／ exit 1
CMD2: /opt/anaconda3/bin/python3 -m unittest tests.test_integration -v
      → Ran 37 tests in 37.864s   ／ FAILED (failures=1, skipped=1) ／ exit 1
```

- 两处的失败是**同一条**：`FAIL: test_01c_ics_independent_validation (…Test01LiveEndToEnd…)`。
- 两处的 `expected failures` 计数均为 **0**，`unexpected successes` 为 **0**；`tests/test_integration.py`
  内**不存在**任何 `@unittest.expectedFailure` / `@unittest.skip` 装饰器（`test_08i` 的预期失败标记已拆除）。
- 唯一 skip 仍是 `test_07b_env_example_placeholders_only`（`.env.example` 不存在，属 deploy 任务范畴）。
- 失败原文（CMD1 日志）：

```
AssertionError: Lists differ: ['DTSTART 未带 TZID=Asia/Shanghai：DTSTART:19[364 chars]009'] != []
First list contains 7 additional elements.
First extra element 0: 'DTSTART 未带 TZID=Asia/Shanghai：DTSTART:19700101T000000'
```

- 同一产物的原始 ICS（`data/t3/outbox/20261008-220104-events.ics`）观测：`VEVENT=3`、`VALARM=3`、`VTIMEZONE=1`，
  其中一条事件被写成 **`DTSTART:19700101T000000`（1970 epoch 回落、无 TZID）**，
  另有三条事件写成**全天**形式 `DTSTART;VALUE=DATE:` / `DTEND;VALUE=DATE:`。
  即 `render.py` 正处在「全天 / VTIMEZONE」改造中途：它既未给定时事件带 `TZID=Asia/Shanghai`，
  又给一条时间未解析的事件落了 1970 年 —— 这是**可观测的产物缺陷**，属 t12 阶段 A 的范围。
- 日志：`data/t11/r3_cmd1_final.log`、`data/t11/r3_cmd2_final.log`；锚点：`data/t11/r3_final_meta.txt`。

### 16.6 回归确认（冻结树，同一 CMD1 日志）

| 项 | 观测 |
|---|---|
| R-1（详情结构化无 TypeError；失败不丢条） | `[R3-R1] exit=0 fetched=1 failed=0 errors=0 structured_time=1 text_only=0 no_time=0 detail_status=complete body_status=ok TypeError_in_stderr=False` |
| D-2（幂等去重、站点唯一 id 全入库） | `[E2E] fetch new=60 pages=2 stop=reached-max-pages ｜ db_items=60 (stats.total_items=60)` |
| R-4（失败模式不静默成功） | `[E2E] enrich={'candidates': 3, 'fetched': 3, 'failed': 0, 'skipped': 0, 'not_found': 0, 'errors': 0, 'error_samples': [], 'structured_time': 0, 'text_only': 2, 'no_time': 1, 'anomalies': []}`；预取/重复页路径 `[R2-PREFETCH] status=202 … 落库=0` |
| R-5（无明确开始时间不写日历） | `[ICS] 无明确开始时间条目 58 条（DB 共 60 条）；正文含『时间待定』=True` |
| 学习收敛（上下界） | `[LEARN] 200×👍 1.6000→2.4978 (max 2.4978 ≤ 5.0)；200×👎 min=-2.6311 ≥ -5.0` |

**修复只影响异常路径**：缺失 `id` 的分支只在「条目缺 `id`」时被触达；
正常路径的落库条数（60/60）、学习权重上下界、ICS 产物文件名与字节数与 round 2 一致（round-2 对照见 §15）。

### 16.7 归因与遗留

1. **契约 ③ 的唯一红灯来自他人所有权的文件** `notice_digest/render.py`（t12 阶段 A）。
   本轮不得修改它（deliver-dev 持锁且阶段 B 未放行），也**不得通过放宽 `test_01c` 断言来消除**。
   该断言自 round 1 写入后未改动，round 2 时该用例为**通过**（round-2 全量 141 项 OK），
   故红灯是 21:30 之后 `render.py` 改造引入的新回归，而非本验证脚本的问题。
2. 21:39–21:40 曾观测到更差的结果（`failures=3, errors=1`，含 `test_render` 的 3 failures + 2 errors），
   那是**文件仍在被写入的过程中**采样的结果，不代表稳定态；冻结态下只剩 1 个失败。
3. 未闭环（供 captain 分派）：`render.py` 需补 `TZID=Asia/Shanghai` 并处理「时间未解析」条目
   （当前落 1970 epoch 或转成全天，二者对 ICS 消费者语义不同）。
4. 未闭环（契约层面的旧账，round 2 已提，round 3 复核仍存在）：
   契约命令「`send --out data/outbox`」实测 exit 2、「`git grep` 扫凭据」在非 git 目录 exit 128；
   以及已废弃的 `page≥43 ⇒ items null` 前提 —— 这些属**任务描述缺陷**，不影响本轮判定。

---

## 17. round 4 = t14 · 投递链复验（ICS 不变式 · 幂等闸门 · 全绿套件）（2026-10-08 晚）

**总结论：pass。** 阶段 B 后投递链的三条契约命令全绿（`Ran 171 tests` / `OK (skipped=1)` / exit 0），
t12 的两处 `render.py` 根因修复经独立复现确认生效，`test_01c` 的断言缺陷经独立裁定后
**带反证**修正（详见 17.2），冻结锚点在验证期间零变动（我的两个 inScope 文件除外）。

### 17.1 本轮范围与冻结锚点

验证开始时（22:35:02）与结束时（22:52:29）各取一次全树 sha256 + mtime，逐字节比对：

| 文件 | PRE sha256 | POST | 判定 |
| --- | --- | --- | --- |
| `notice_digest/store.py` | `89d23240…` | 一致 | 冻结 |
| `notice_digest/cli.py` | `9e230995…` | 一致 | 冻结 |
| `notice_digest/render.py` | `92ef9ae8…` | 一致 | 冻结 |
| `notice_digest/mailer.py` | `f0f57248…` | 一致 | 冻结 |
| `tests/test_render.py` | `75041f05…` | 一致 | 冻结 |
| `notice_digest/enrich.py` / `score.py` / `timeparse.py` / `feedback.py` / `tests/test_repair_round2.py` | — | 一致 | 冻结 |
| `tests/test_integration.py` | `9551c7d2…` | `e569de2f…` | **变动，属本任务 inScope**（PATCH 1–5 + ICS 校验器） |
| `tests/verify_notes.md` | `7bdcd6d7…` | 本文件追加 §17 | **变动，属本任务 inScope** |

五个 t12 锚点（render/store/cli/mailer/test_render）**逐字节未变**，其余非本人所有权文件亦未变
⇒ 本轮全部结论都建立在冻结树上，不存在 §16 提到的「读到中间态」采样风险。
证据：`Vesper缓存/临时工作区/nd_r4/anchors_pre.txt`、`anchors_post.txt`。

### 17.2 `test_01c` 独立裁定（本轮唯一的断言修改，附反证）

**争议**：实现方主张「原断言『每条 DTSTART/DTEND 都必须带 `TZID=Asia/Shanghai`』对任何合规日历都不可能成立」，请求放宽。
裁定权归测量方；我不采信其主张，自行推导后给出**分层裁定**：

1. **原断言确实不可能被「含全天条目或含 VTIMEZONE 的日历」满足**（这部分实现方是对的）：
   RFC 5545 §3.3.5 明定 TZID 参数**不得**用于 `VALUE=DATE` 值 ⇒ 全天条目的
   `DTSTART;VALUE=DATE:` / `DTEND;VALUE=DATE:` 合规写法就是不带 TZID；
   §3.6.5 明定 VTIMEZONE 内 STANDARD/DAYLIGHT 的 `DTSTART` 是**该时区的本地时间**，同样不带 TZID。
   本产品的真实产物**必然包含全天条目**（活体抽样 60 条里 58 条无明确开始时间 ⇒ 落全天），
   即该断言禁止的是产品**被要求产出**的输出 ⇒ **缺陷在断言，不在 `render.py`**。
2. **但实现方「对任何合规日历都不可能」的措辞过宽**：一个只含 TZID 定时事件且不含 VTIMEZONE 的日历
   是可以满足原断言的。因此这不是「规范上不可能」，而是「与产品必需输出冲突」——**同一结论，但对范围的表述必须收紧**，
   否则会滑向「凡红灯皆断言错」。
3. 原断言另有**方向性缺陷**：它只查「有没有 TZID」，**不查**「裸参数/浮动时间」；而裸的
   `DTSTART:20261010T140000`（浮动时间）同样违规却不会触发原断言 ⇒ 原断言既**过严**（误伤全天与 VTIMEZONE）
   又**过松**（漏掉浮动时间）。修正后的校验器严格更强。

**修正范围**（依 captain 的 amend，作用域收到 VEVENT 级）：跳过 `VALUE=DATE` 行与 VTIMEZONE 块；
仅在 VEVENT 内对**定时** `DTSTART` 强制 `TZID=Asia/Shanghai`；全天行带 TZID 反而报错；
违规计数必须为 0。修正后 `violations` 计数进入断言。

**强制反证（项目目录外的副本，未通过则本项判 fail）**：
`rsync` 出项目外副本 `Vesper缓存/临时工作区/nd_r4/negctl_tzid/`（含 PATCH 后的测试），
把 `notice_digest/render.py` 中唯一一处定时 DTSTART 的 `;TZID={TZID}` 删掉（即人为注入被断言保护的缺陷），
再跑同一测试：

```
NEGCTL_EXIT=1
Ran 3 tests
FAILED (failures=1)
AssertionError: Lists differ: ['第 23 行 定时 DTSTART 必须带 TZID=Asia/Shanghai[42 chars]000'] != []
  '第 23 行 定时 DTSTART 必须带 TZID=Asia/Shanghai（裸参数 / 浮动 / UTC 均不合规）：DTSTART:20261010T140000'
```

⇒ 修正后的断言**变红并指名违规行号与违规原文**，只有 `test_10a` 变红（10b/10c 仍绿，说明反证精准命中 ICS 不变式）。
日志：`nd_r4/negctl_run.log`。**这条反证是本项修改成立的唯一依据**——没有它，改断言就是「改测试换绿灯」。

### 17.3 ICS 不变式实测（独立探针，计数取自真实 `test_10a` 产出）

探针在进程内 hook `render_mod.render_ics` 捕获真实产物（非重写、非复制），再独立解析：

| 指标 | 实测值 |
| --- | --- |
| 换行 | 仅 CRLF（`
`），裸 CR = 0 |
| VEVENT 数 | 2 |
| DTSTART 行（全文件） | 3 = 1 定时 VEVENT + 1 全天 VEVENT + 1 VTIMEZONE 内本地时间 |
| VEVENT 内定时 DTSTART（带 `TZID=Asia/Shanghai`） | 1 |
| VEVENT 内全天 DTSTART（`VALUE=DATE`） | 1 |
| VEVENT 内 DTEND | 2 |
| VALARM | 2 |
| VTIMEZONE | 1 |
| 全天 DTSTART 行误带 TZID | 0 |
| 违规数（VEVENT 级） | 0 |
| 文本含 `1970` | 0 |
| VTIMEZONE 是否在首个 VEVENT 之前 | 是 |

原始行（逐字）：

```
DTSTART:20250101T000000                      ← VTIMEZONE（本地时间，合规无 TZID）
DTSTART;TZID=Asia/Shanghai:20261010T140000   ← 定时事件
DTEND;TZID=Asia/Shanghai:20261010T160000
TRIGGER:-PT30M                               ← 定时事件闹钟
DTSTART;VALUE=DATE:20261010                  ← 全天事件（不得带 TZID）
DTEND;VALUE=DATE:20261011
TRIGGER;VALUE=DATE-TIME:20261009T233000Z     ← 全天事件闹钟（= 当日 07:30 本地）
```

**t12 两处根因修复的独立复现**（不读源码字符串，只看产物）：
(a) VTIMEZONE 纪元兜底已生效——上表第一行 `DTSTART:20250101T000000`（当年 2025 = now.year−1），
全文件零 `1970`；旧版本的 epoch 兜底缺陷确已消失。
(b) VTIMEZONE 块**先于**首个 `BEGIN:VEVENT`（探针直接比较两个偏移量，返回 `True`）。

### 17.4 幂等闸门（三方证据，台账行数不作为判据）

`test_10b` 在同一日内连跑三次 `cmd_send`，SMTP 调用被 spy 计数：

| 轮次 | SMTP 累计调用 | 关键观测 |
| --- | --- | --- |
| 1 | 1 | 正常投递，`summary.sent=True` |
| 2（同内容） | 1（**不增**） | `summary.gated=True`、`sent=False`、无 `[mailer] 已投递`，stderr 出现闸门专属行，含 32 位十六进制内容指纹 |
| 3（内容变更） | 2 | 指纹与第 1 轮不同 ⇒ 闸门按**内容**放行 |

gate 专属 stderr（实测，含指纹与首次投递时间字段）：

```
[send] 幂等闸门命中：2026-10-08 已投递过同一份内容（指纹 <32hex>），本次不发信、不重复记台账。
```

三方证据同时成立：① SMTP 真实调用数 1→1→2；② 闸门专属日志（带内容指纹，非仅日期）；
③ receipt 文件数 1→2；另 `send_attempts` 中 `sent` 行恰为 2 行（attempt 行在 SMTP 之前写、
`sent` 仅在成功后置位）。
**台账 `sends` 行数不作为证据**：其 `date` 是 PRIMARY KEY 且走 UPSERT，行数恒为 1，用它判幂等会得到假绿。

### 17.5 窗口语义（严格 `> last_sent_at()`）

`test_10c` 用纯 DB 构造，不受上游影响：

| 轮次 | 库内条目 | 结果 |
| --- | --- | --- |
| 1 | 2 条「3 天前」 | exit 0，1 次投递，`n_items=2`，`window_since=None`（首轮窗口为空） |
| 2 | 同前（无新增） | exit 0，**不投递**，`sent=False`，`window_since` 非空，stderr 出现「空主题哨兵」，无 `[mailer] 已投递` ⇒ 返回 0、**不写台账行** |
| 3 | 增 1 条 `now+2m` | 累计 2 次投递，`n_items=1`（**只计窗口内新增**，3 天前两条不被重复计数）|

正文交叉验证：三封 HTML 中 `data-nd-item=` 计数依次为 2 / — / 1，第三封含新条目标题、不含旧标题。

**残留观察（不作为缺陷判 fail，供后续任务决定是否改）**：`cmd_send` 先按窗口过滤，再在非空集合上判定内容闸门；
而窗口比较是**严格大于**上次投递时刻，正常日期的条目在当日二次运行时会被窗口过滤掉，
于是走「空主题哨兵」分支返回 0，**根本到不了内容闸门**。因此 17.4 的闸门只有在窗口内仍有条目
（如未来日期条目）时才可达。安全性不受影响（同日重复运行确实不会二次发信，双层保护都在），
但「内容级闸门」在常规数据下是**不可达的冗余保险**——这是行为事实，非本轮验收失败项。

### 17.6 D-3（缺 id 的 schema 漂移）三态复现

| 场景 | exit | `stop_reason` | `skipped_items` | 失败邮件 | 异常级别 |
| --- | --- | --- | --- | --- | --- |
| 全缺 id | 3 | `page-1-schema-drift` | 1 | 有 | `schema-drift` = error |
| 部分缺 id | 0 | 正常收敛 | 1 | 无 | `item-missing-id` = warn |
| 真零新增（对照） | 0 | `page-1-all-known` | 0 | 无 | 无 error 级 |

三者**可区分**（这正是 D-3 关闭的判据）：全缺 ⇒ 报错 + 退出码 3 + 失败邮件；
部分缺 ⇒ 只警告 + 退出码 0 + `skipped_items` 可见；真零新增 ⇒ 静默成功、不发失败邮件。
原始证据行：`[R3-D3-FULL]`、`[R3-D3-PARTIAL]`、`[R3-D3-CONTROL]`（见 `nd_r4/cmd1_final.log` 第 277/279/281 行）。

### 17.7 回归项（原始输出为据）

- **R-1（活体 E2E）**：`[R2-EXITCODE] page-1-empty → exit=3`；活体抓取 `new=60 pages=2 stop=reached-max-pages`，
  `[DEDUP] 唯一 id 60；库中已在 60；缺失 0`；二次抓取 `new=0 known=30 stop=page-1-all-known`（整页已知即收敛）。
- **D-2（详情/正文）**：`[R3-R1] exit=0 fetched=1 failed=0 errors=0 structured_time=1 text_only=0 no_time=0
  detail_status=complete body_status=ok TypeError_in_stderr=False`。
- **R-4（失败路径）**：`[FAIL-2]` detail 404 不炸；`[FAIL-3]` SMTP 抛异常 ⇒ `send=False`；
  `[FAIL-3b]` 缺凭据 ⇒ `send=False`（不静默成功）。
- **R-5（反馈签名）**：`[FAIL-4] 未签名=403 / 伪签名=403 / 错 kind=400 / 点击换 kind=403`。

### 17.8 凭据扫描（仓库级，排除 `data/`）

全树扫描硬编码密钥/口令模式，命中全部为**占位符或文档字符串**，无真实凭据、无 `.env`：

- `tests/test_render.py:245` `"SMTP_PASS": "unit-test-not-a-real-credential"`
- `tests/test_render.py:329` `smtp_pass="cfg-unit-test-credential"`
- `tests/test_integration.py:91` `"ND_SMTP_PASS=t3-verify-placeholder"`
- `notice_digest/config.py:4` 文档字符串、`docs/RUNBOOK.md:28` 说明文字

另需注意（沿用 §16 的旧账，属**任务描述缺陷**而非实现缺陷）：契约里的「`send --out data/outbox`」实测 exit 2、
「`git grep` 扫凭据」在非 git 目录 exit 128 —— 这两条本轮**已从契约移除，不再执行**。

### 17.9 契约命令结果

| # | 命令 | 结果 |
| --- | --- | --- |
| 1 | `python3 -m unittest discover -s tests -v` | `Ran 171 tests in 94.861s` / `OK (skipped=1)` / exit 0 |
| 2 | `python3 -m unittest tests.test_integration -v` | `Ran 40 tests in 109.585s` / `OK (skipped=1)` / exit 0 |
| 3 | `python3 -m notice_digest.mailer --dry-run --selftest --out data/r4_outbox` | 落盘 6 文件（12 条）/ 主题「清华通知日报 10-09｜明日 3 场活动，1 项报名今日截止」/ exit 0 |

**唯一 skip 的正当性**：`skipped=1` 来自 `test_07b` 的**运行时** `skipTest`（`.env.example` 尚未创建，
属 deploy 任务范畴）。全 `tests/` 目录内**不存在**任何 `@unittest.expectedFailure` / `@unittest.skip` 装饰器
（`test_08i` 的旧预期失败标记已在 §16 拆除）⇒ 无「用装饰器把红灯藏成绿」的情形。
阶段 A 的旧基线为 `Ran 168 tests` / `FAILED (failures=1, skipped=1)`（唯一失败 `test_01c`），
本轮 171 项全绿，新增 3 项即 17.3–17.5 的 round-4 用例。

### 17.10 本轮残余与未闭环（交 captain 分派）

1. **内容级闸门在常规数据下不可达**（17.5 末段）：双层保护使安全性无损，
   但若希望闸门在「同日重跑」时真正生效，需把窗口比较改为 `>=` 或让闸门先于窗口过滤判定。
   属设计选择，非缺陷，需产品方拍板。
2. `test_01c` 的断言修正已带反证（17.2），但**反证只在本地副本中做过**；项目树中的断言自此为普通断言，
   后续任何改动都应重跑一次同款反证。
3. 契约层的三条旧账（17.8 末段）已在本轮契约中移除，若历史文档仍引用旧命令需一并更正。

### 17.11 D-3 反证对照（本轮重跑 · 副本位于项目目录之外）

判据：把 D-3（缺 `id` 的 schema 漂移）修复**人工回退**后，该组用例必须变红。
若回退后仍全绿，说明断言被放宽 —— 本轮即为防这一点而重跑。

副本：`Vesper缓存/临时工作区/nd_r4/negctl_d3/`（项目目录之外，`rsync -a` 自项目树复制，
排除 `data/`、`__pycache__`、`*.pyc`），仅回退 `notice_digest/store.py` 中一处
`skip_sink["missing_id"]` 计数逻辑（`REVERTED anchors=1`），其余文件与原树逐字节相同。

```
cd <nd_r4/negctl_d3>
/opt/anaconda3/bin/python3 -m unittest tests.test_integration.Test09Round3Closure -v
→ Ran 4 tests in 0.029s
→ FAILED (failures=2)                                  NEGCTL_D3_EXIT=1
```

| 用例 | 结果 | 实际失败原文 |
| --- | --- | --- |
| `test_09a_all_items_missing_id_is_fatal_not_silent` | **FAIL** | `AssertionError: 0 != 3 : 整页零可用必须判致命（exit 3）` |
| `test_09b_partial_missing_id_keeps_rows_and_reports_skip` | **FAIL** | `AssertionError: 0 != 1 : 跳过条数必须在 JSON 里可读` |
| `test_09c_genuinely_zero_new_is_distinguishable` | ok | 真·零新增本就不依赖该修复 |
| `test_09d_enrich_structured_detail_has_no_typeerror` | ok | R-1 回归，与本修复无关 |

回退后的行为面证据（与 17.6 的正常态对比，缺口可见）：

```
[R3-D3-FULL] exit=0 new=0 known=0 skipped_items=0 stop=page-2-repeated rows=0
             failure_mail=0 anomalies=[('page-repeat','warn'),('zero-new','warn')]
[R3-D3-PARTIAL] exit=0 new=1 rows=1 skipped_items=0
                anomalies=[('max-pages-hit','warn')] failure_mail=0
[R3-D3-CONTROL] exit=0 new=1 / 二次 exit=0 new=0 skipped_items=0 stop=page-1-all-known
                error级anomaly=[] failure_mail=0
```

对照 17.6 的正常态（`[R3-D3-FULL] exit=3 … stop=page-1-schema-drift … failure_mail=1`、
`[R3-D3-PARTIAL] … skipped_items=1 … anomalies=[('item-missing-id','warn'),…]`）可确认：
**修复与否在 exit 码、stop 原因、skipped 计数、失败邮件四项上都可观测地不同** ⇒
该组断言仍是有效门禁，未被放宽。反证日志：`nd_r4/negctl_d3_run.log`。

正交证据（另一条独立反证，见 17.2）：`test_01c` 的 ICS 断言修正也已在项目外副本
`nd_r4/negctl_tzid/` 中做过同款反证（注入裸定时 `DTSTART` → `NEGCTL_EXIT=1`、
`FAILED (failures=1)`、指名 `第 23 行 … DTSTART:20261010T140000`）。两条反证互相独立。
---

## 18. round 5 = t15 · 文档口径清理（只清理引用，不改任何结论）

本轮的改动**只限**「把已失效的引用就地标注为历史 / 把时效性措辞改为常设口径」，
§0–§17 的 **verdict、哈希、行号、计数、原始输出**一律未动（逐行 diff 见任务 t15 报告）。依据 §17.10 第 3 条
「若历史文档仍引用旧命令需一并更正」：

| 类别 | 处理方式 | 位置 |
| --- | --- | --- |
| `items=null` / `page≥43 ⇒ null` 前提（t8 已证伪） | 就地标注「历史前提已证伪，见 §15.1」 | §0 ⑤、§5、§11 D-1（标题 + 原始输出行）、§14 末、§14.4 两处 |
| 已移除的 verify #5 `git grep` | 就地标注「已在后续轮次从契约移除，见 §17.9」 | §8、§10 节标题、§14.6、§15.9 |
| 已移除的 verify #4 `send … --out` | 同上 | §9 表、S-2（§11） |
| 「本轮不再依赖…」的时效措辞 | 改写为**常设口径** | §15.1 末、§15.5 末 |

**常设口径（取代上述所有旧措辞）**：站点**不存在**「page=43 起 `items` 为 null」；收敛只能由
三条件之一判定（整页 id 已知 / 与上页 id 相同 / 触顶），**不得断言任何具体页数**；
日增也不是 150–180 条（实测**均值约 5 条/日、峰值 112 条**）。数据源实测依据见 t8 复现记录。

本节为**追加**内容；本条不改变 §0–§17 的任何判定与证据。

---

## 19. t16 对抗式审查结论（reviewer · attempt 750d4b5c-87c6-45a9-a9fe-09909db99b68）

**判定：pass（6/6 验收项通过）**。本节由审查方在**收官锚点采集之后**追加，属声明性写入：
append 前本文件 sha256 = `4fd5dd9a4e5be6a1834b3eff8ccd19d30d21b9513223bed27e3e4d12b9474fb8`；append 后 = `0f935aad003d0a15cb0473d4ca74b9b96fb7d44bbb3970cca51471ed51f027a2`。审查窗口内 6 个冻结文件零变动。

### 19.1 冻结与锚点（验收 1）
- 开工锚点 `Vesper缓存/临时工作区/nd_review/t16/anchors_start.txt`（9 文件 sha256[:16]）；收官 `anchors_end.txt`，逐字节比对：
  `notice_digest/mailer.py` `46e7dee9ce502169` · `notice_digest/store.py` `60387ea542502cb7` ·
  `notice_digest/render.py` `9c81c25c6a1a3dc5` · `tests/test_integration.py` `73aea3502b558182` ·
  `docs/RUNBOOK.md` `3278fec1709724afa1dde48d50ec1e1065338bc2dea850283fee542c7dfae5ba` ·
  `tests/verify_notes.md` `4fd5dd9a4e5be6a1834b3eff8ccd19d30d21b9513223bed27e3e4d12b9474fb8` —— **全部一致**。
- 测量期唯一非冻结副作用：`python3 -m unittest` 重建 `__pycache__`（非 inScope 文件、内容无变更）。

### 19.2 F1：正文相同、渲染时刻不同 ⇒ 不得二次实发（验收 2）—— 通过
项目目录外独立临时库（`nd_review/t16/led_S1`），条目集合不变、只变运行时刻：

| 轮 | 时刻 | 本轮 SMTP 连接数 | disp | 指纹 |
|---|---|---|---|---|
| R1 | 07:30 | 1 | **1** | `a7532d0d2a1d5979babdc305d94174e4` |
| R2 | 07:31（+61s）| **0** | **0** | `a7532d0d2a1d5979babdc305d94174e4`（逐字相同）|
| R3 | 19:00 | 0 | 0 | 同上 |
| R4 | 23:59 | 0 | 0 | 同上 |

R2 原始错误流：`[send] 幂等闸门命中：2026-10-09 已投递过同一份内容（指纹 a7532d0d2a1d5979babdc305d94174e4），本次不发信、不重复记台账。`
render 层纯度补充：同一份 scored/parsed 在 07:30 / 12:00 / 19:00 / 23:59 四次渲染 → 指纹恒为
`6c082e16b48908ace3792c5284809b16`，21 行正文**非页脚差异 0 行**，ICS 长度恒 3691 字节。
⇒ 「渲染时刻进入指纹」的旧漏口已关闭；criterion 2 的 needs_revision 触发条件（同内容二次实发）未出现。

### 19.3 F2：NULL 行的显式语义与同日重复（验收 3）—— 通过（残余已逐字归类）
- 显式语义：`store.py` 窗口 SQL 给 `published_ts IS NULL` 行独立的当日下界（`day_start_jd`）；docstring 明示
  「同日后续若另有新条目而重算正文，该 NULL 行会再随那封信出现一次」。
- 纯 NULL 行同日 4 轮（S1）：disp **1/0/0/0** —— t13 记录的「无上界 1/1/1」已消除。
- 混窗 5 轮（S2，P1 于 07:31 退窗）：

| 轮 | 时刻 | 窗口 | disp | 指纹 | 主题 |
|---|---|---|---|---|---|
| R1 | 07:30 | [P1,N1] | 1 | `d96e48062456f8645907b7cd1fe50ab1` | 新增 2 条 |
| R2 | 07:31 | [N1] | 1 | `613b200468fa63ac4801c3cdbcf2c556` | 新增 1 条 |
| R3 | 07:32 | [N1] | **0** | `613b200468fa63ac4801c3cdbcf2c556` | 闸门命中 |
| R4 | 07:41 | [P2,N1] | 1 | `4140d92503f7b4aff62cf85542717d12` | 新增 2 条 |
| R5 | 07:42 | [N1] | **0** | `613b200468fa63ac4801c3cdbcf2c556` | 闸门命中 |

- **同一份内容被二次实发：0 次**（相邻两轮 disp=1 的指纹两两不同）。允许的残余（**逐字归类为残余、非缺陷**）：
  R2 那封信的正文集合因 P1 退窗而与 R1 不同，N1（NULL 行）随之第二次出现在邮件正文里 —— 该行为已在
  `store.py` docstring 显式定义语义，上界 = 当日窗口集合的变化次数（实测：NULL-only 1/0/0/0；混窗 3 次实发后收敛）。

### 19.4 四失败模式可达性图（验收 4）
| # | 失败模式 | 由哪道闸拦下 | 代码 | 实测 disp |
|---|---|---|---|---|
| 1 | 同日重跑（正文相同、无新条目）| store 内容指纹幂等闸门 `begin_send`（同 `(date,fingerprint)` 已 `sent` ⇒ 返回 False）| `store.py:457-461`（docstring 明示）；调用点 `cli.py:440-442` | **0**（+61s / +11.5h / 跨 23:59 三处；日志 `[send] 幂等闸门命中`）|
| 2 | 崩溃于 SMTP 之后（`mark_sent` 未执行）| 投递窗口下界只认**成功投递**（`store.py:559-567`「attempt 行不算」）⇒ 下一轮窗口为空 ⇒ 空内容哨兵 | `store.py:559-567`、`cli.py:431`（空主题哨兵，不发信不记台账）| R1 **1**（台账停在 `attempt`）→ R2 **0**（SMTP 连接 0，退出语「当天没有新条目（空主题哨兵）」）|
| 3 | 崩溃于 SMTP 之前（SMTP 失败）| 无需闸：窗口下界不推进 ⇒ 不丢条目；`attempt` 行不阻断重试 | `cli.py:451-454`（仅 `ok` 才 `mark_sent`）| R1 rc=4 **0**（窗口仍含该条目）→ R2 **1**，指纹与 R1 逐字相同 `540b15a1091c3787e3bc9b169e17fb15`，台账终态 `sent` |
| 4 | 同日出现新条目 | 指纹随正文变化 ⇒ 闸门**按设计失配**、允许发信；紧随的同内容重跑被拦 | 同上 #1 | S2 R4 **1** → S2 R5 **0** |

台账写入顺序与 `sends` 行数均不作为判据；本表全部以 SMTP 实发次数 + 闸门日志 + 指纹为准。

### 19.5 反证复核（验收 5）—— 通过
项目目录外两棵单点回退树（各只改一个文件、其余与冻结树逐字节相同）：
- `nd_review/t15_red/red_f1`（仅去掉 mailer 的页脚归一化）：`Ran 4 tests … FAILED (failures=2)`，指名
  `[T15-F1] 页脚渲染时刻不得进入指纹：同内容跨分钟必须得同一指纹`（得 `c3400675e287ab3c573a1fe3800d6552`）
  与 `test_11b_cross_minute_rerun_of_same_content_must_not_resend` → `AssertionError: 2 != 1`。
- `nd_review/t15_red/red_f2`（仅把 store 窗口条件退回 `published_ts IS NULL OR …`）：`FAILED (failures=1)` →
  `test_11c_…` `AssertionError: 't15:f2:null' unexpectedly found in ['t15:f2:null']`。
- 冻结树同名类：`Test11IdempotencyKeyPurity` `Ran 4 tests … OK`。
- 既有断言未被删除或弱化：`tests/test_integration.py` 断言计数 **260 → 318**，删除 assert 行 **0**、新增 **58**。

### 19.6 文档越界核查（验收 6）—— 通过
- `docs/RUNBOOK.md`：共 4 行变更（2 删 2 增），删除的两条是**已被 t8 实测证伪的前提**
  （「约 page=43 起 `items` 为 null。」「日均 150–180 条；停机超过 2 天需临时提高 --pages」），替换为更正口径；
  **未触碰任何证据结论、sha256 锚点或计数**。
- `tests/verify_notes.md`：17 非空行删 / 32 非空行增（含纯空行 hunk 时为 18/38）。18 条删除行中 9 条与现存最佳匹配行
  相似度 <0.85，逐条核对后全部属三类：① 现存行系原文**前缀**（后接 `〔历史…〕` 标注，如 ⑤ suspect 行、§D-1 标题行）；
  ② 措辞由「本轮」升格为「本文件任何轮次」（更强，非放宽）；③ 标题/编号重排（如 `## 10. 环境事实（本轮核实）` 归并）。
  **未发现任何被删除的证据结论、判定词（pass/fail/suspect）或计数。**
- 「17 处历史标注」独立计数：§18 表格 **13 处**、含「历史」的行 **14 行**、`〔历史` 标记 **9 处**。17 与三者均不符，
  判为**计数口径不同**（无法从文本复现），**不是漏标** —— 逐条映射显示 §18 列出的 13 处位置均带标注。

### 19.7 残余（明示非缺陷）
1. F2 残余（见 §19.3）：NULL 行在「窗口集合变化 ⇒ 正文变化」时随新一封再次出现，上界 = 当日窗口集合变化次数。
2. 崩溃于 SMTP 之后、且回执写入之前崩溃：退化为「窗口已推进、本轮不重发」（宁可漏发不重复发）——设计取舍。
3. 全量套件本轮实测 `Ran 175 tests in 17.9s / FAILED (failures=4, errors=2, skipped=1)`：`test_01a`/`test_01b`/
   `test_02a`/`test_05c` 失败、`test_01e`/`test_02b` 报错，均依赖上游 pkuknow.cn 直连（HTTP 403，环境/上游因素，
   captain 已独立核实），**不计入本任务判定**。（captain 口径为 4 红，本轮实测 6 红，差异来自同为上游依赖的
   `test_01a`/`test_01b` 两个 live 用例。）
