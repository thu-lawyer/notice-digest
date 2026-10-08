# 修复轮 2（t7）改动与实测证据

## 1. 背景

第一轮独立评审提出 T4-F1…F7，随后复核提出阻塞项。本轮修复引擎侧缺陷并补齐可观测性；
**冻结的对外接口与调用签名不变**（渲染层与投递层无需改动）。

## 2. 逐项处置

| 文件 | 问题 | 处置 |
| --- | --- | --- |
| `notice_digest/enrich.py` | R-1：`enrich_pending` 内局部变量与返回字段同名，把整数当函数调用 → `TypeError`，整批中断 | 局部计数改名（`structured_cnt`/`text_cnt`/`none_cnt`），对外返回键保持兼容 |
| `notice_digest/enrich.py` | R-5：单条详情 404 抛错导致整批终止 | `enrich_one` 捕获 `FetchError`；404 记 `not_found` 并 `touch_enrich_attempt` 后继续，计数进入输出 |
| `notice_digest/cli.py` | 末页重复被当致命错误；异常分级不清；首页为空静默通过 | 末页重复降为 warn（先判 all-known 再判 repeated，页签名在 upsert 前计算）；退出码分级明确；`page-1-empty` 升为 error |
| `notice_digest/feedback.py` | D-2：合法反馈被预取规则误吞（跨线程连接复用 + UA 名单过宽） | 线程本地惰性建连 + 写锁；UA 收窄为「已知邮件扫描器/代理/爬虫」；预取丢弃改为可观测（202 + `X-ND-Skipped: prefetch` + stderr 日志） |

## 3. 本轮遵守的裁决

1. **D-2 路由**：线程本地惰性创建连接 + 写锁（不要求 `check_same_thread=False` 字面量）。
2. **UA 收窄是显式规则**：只有已知邮件扫描器/代理/爬虫签名算预取；通用 HTTP 客户端库名不算证据；
   显式反馈被丢弃必须可观测（状态码或响应头 + 日志），禁止静默吞掉。
3. **末页重复属正常**：越界页被钳制回最后一页 → warn + 正常收敛，退出码 0，不发失败邮件。
4. **退出码分级**：只有「网络/5xx 重试后仍失败、非 JSON、结构漂移、首页为空」才 exit 3 并触发失败邮件。

## 4. 实测原始输出（修复后、同一轮改动之后复跑）

### R-4 末页重复（3 页，第 3 页与第 2 页同批 id）

```
exit_code=0
[fetch][warn] page-repeat: page 3 与已扫描页返回同一批 id（越界钳制），且整页已全知，按 all-known 收敛
{"pages_scanned":3,"new":4,"known":2,"stop_reason":"page-3-all-known","repeat_detected":false}
anomalies=[('page-repeat', 'warn')]   failure_mail=None   failure_mail_attempted=no
```

### 幂等复跑（同一库连跑两次）

```
run2: exit_code=0
[fetch][warn] zero-new
{"pages_scanned":1,"new":0,"known":2,"stop_reason":"page-1-all-known"}
failure_mail=None
```

### 致命分级（每条均为 exit_code=3 且已尝试发失败邮件）

```
网络 5xx 重试耗尽: FAILURE [fetch] FetchError: 网络请求失败: HTTP 503 Service Unavailable（重试 3 次后仍失败）
非 JSON 响应:     FAILURE [fetch] FetchError: 响应不是合法 JSON: Expecting value: line 1 column 1
结构漂移(顶层):   FAILURE [fetch] structure-drift: page 1 返回 str，期望 dict      stop_reason=page-1-bad-payload
结构漂移(items):  FAILURE [fetch] structure-drift: page 1 items 类型异常：str
首页为空:         FAILURE [fetch] page-1-empty: 第 1 页 items 为空 —— 上游可能改结构或不可用，不能当成静默成功
                  stop_reason=page-1-empty
```

### R-5 单条详情 404 不中断整批

```
exit_code=0
[enrich][warn] detail-404: 1 条详情 404（列表挂着但详情已下架），已计数并留待重试，不影响其它条目
{"candidates":3,"fetched":2,"failed":1,"skipped":0,"not_found":1,"errors":0,
 "structured_time":0,"text_only":2,"no_time":0,"error_samples":[]}
anomalies=[('detail-404', 'warn')]   failure_mail=None
```

### D-2 反馈可观测性

| 请求 | 结果 |
| --- | --- |
| 预取 UA（GoogleImageProxy）打 `/nd/f` | HTTP 202 + `X-ND-Skipped: prefetch` + stderr 一行忽略日志；feedback 行数 0 → 0 |
| 普通 UA（`Python-urllib/3.12`）打 `/nd/f` | HTTP 200；feedback 行数 0 → 1（权重随之变化） |
| 点击 `/nd/c` | HTTP 302，`Location` 完好，feedback 行数继续增加 |

## 5. 回归

```
python3 -m unittest discover -s tests
Ran 132 tests in 35.382s
OK (skipped=1)
```

## 6. 本轮未改动

`fetch.py`、`store.py`、`render.py`、`mailer.py`，以及各模块公开签名（`config` / `store` / `fetch` / `enrich_pending` /
`timeparse` / `score` / `feedback`）均未变更。
