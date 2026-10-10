# thu-push

把经**微信读书接口**订阅抓取的公众号新文章，做 **AI 精选排序**后，每天定时邮件推送给你。

抓取与入库由自托管引擎 [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss) 完成（经微信读书接口订阅公众号、定时增量同步）；本工具**只读**引擎数据库，在其上补齐 AI 精选、跨源去重、邮件渲染与幂等投递。

> 2026-10 起本仓库由 notice-digest 更名为 **thu-push**，并整体并入原独立仓库 weread-push（微信公众号订阅导入与引擎运维工具链，见 [`weread-push/`](weread-push/) 子目录，其文档独立维护）。
>
> 数据源沿革：早期版本基于 pkuknow.cn 校园通知聚合站做「通知日报」；**现已不再从聚合站取数**，数据全部来自微信读书接口（见 §2）。原通知链路（`fetch` / `enrich` / 个性化反馈）作为可选组件保留。

---

## 1. 它做什么

```
we-mp-rss 引擎（微信读书接口订阅，定时增量抓取入库）
        ↓  只读 SQLite（ND_GZH_DB，以 file:...?mode=ro 打开，绝不写入引擎库）
gzh_source 取增量（回看窗口 ND_GZH_LOOKBACK，默认 26 小时）
        ↓  智谱 LLM 精选排序（取前 ND_GZH_TOP 篇；LLM 失败 → 内置关键词权重表兜底）
跨源去重（与本地通知库按归一化标题比对，同名 → 通知优先）
        ↓
render（HTML 邮件；通知侧另产 .ics 日历附件）  →  send（SMTP 投递 + 幂等闸门）
```

- **gzh_source**：公众号文章源。文章按 URL 的 md5 记入独立台账（`state-gzh.json`，与通知侧台账互不影响），**发信成功后才记账** —— 重复运行、中途失败都不会把同一篇推两次。
- **排序**：调用智谱 LLM 从候选中选出每日精选；LLM 不可用时自动降级为内置关键词权重表（讲座、法学、AI、清华等），投递不会因 LLM 故障中断。
- **跨源去重**：公众号文章与本地通知条目按归一化标题比对，同一内容只出现一次。
- **send**：SMTP 投递；同日同内容有幂等闸门，防重复发信。

另有 `feedback-serve`：只监听回环地址的反馈服务，接收邮件里的 👍/👎 点击。

---

## 2. 数据来源与抓取礼仪

- **当前数据源：微信读书接口（经 we-mp-rss 引擎）**
  - 引擎自托管，并自行维护微信读书登录态；本仓库不存储、不经手任何微信凭据。
  - 本工具对引擎数据库**只读**（`file:...?mode=ro`），不直接请求任何微信接口；抓取频率与礼仪由引擎侧控制。
  - 增量窗口默认 26 小时；每封邮件精选篇数默认 10（`ND_GZH_TOP`）。
- **订阅导入礼仪**（`weread-push/import/mp_searchbiz.py`）：通过微信 `searchbiz` 接口批量发现公众号（CDP 真实浏览器通道），**≥ 2.5 秒/次限速**、幂等去重；仅用于订阅导入，不做并发请求。
- **订阅清单**：132 个公众号见 [`weread-push/feeds/feeds-132.json`](weread-push/feeds/feeds-132.json)（仅公开名称与公开 biz 标识，无隐私数据）。
- **历史数据源（已停用）**：pkuknow.cn 校园通知聚合站。其接口约束与抓取礼仪的实测记录保留在 `docs/RUNBOOK.md`；`fetch` / `enrich` 子命令仍可运行，作为可选的通知补充源。

---

## 3. 安装

依赖极简：**Python 标准库 + `requests`**。

```bash
git clone https://github.com/thu-lawyer/thu-push.git
cd thu-push

python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt

cp .env.example .env && chmod 0600 .env   # 然后手工填入 SMTP 账号与授权码
```

要求 Python ≥ 3.10（服务器实测 3.12.3）。

---

## 4. 配置

优先级：**环境变量 > `.env` > `profile.yaml` > 内置默认值**。全部省略也能跑（会因缺少收件地址而无意义，但不会崩）。

### 4.1 `.env`（密钥与账号，永不进 git）

| 键名 | 说明 |
| --- | --- |
| `ND_TO_ADDR` | 收件地址 |
| `ND_FROM_ADDR` | 发件地址（通常同 SMTP 账号） |
| `ND_SMTP_HOST` | SMTP 服务器（**不要用 `smtp.tsinghua.org.cn`**，该域名是 CNAME 别名、证书不匹配，直连会因证书校验失败而无法投递） |
| `ND_SMTP_PORT` | 端口，465 = SSL 直连 |
| `ND_SMTP_USER` | SMTP 登录账号 |
| `ND_SMTP_PASS` | SMTP 授权码（**唯一必须手工填的敏感项**） |
| `ND_GZH_DB` | we-mp-rss 引擎 SQLite 路径（**配置后公众号源启用**；留空则只走通知链路） |
| `ND_GZH_STATE` | 公众号已发送台账路径（缺省 = `ND_GZH_DB` 同目录的 `state-gzh.json`） |
| `ND_GZH_TOP` | 每封邮件 AI 精选篇数（默认 10） |
| `ND_GZH_LOOKBACK` | 公众号增量回看窗口，小时（默认 26） |
| `ND_ZHIPU_API_KEY` | 智谱 API key（公众号排序用；缺省 → 关键词兜底，不影响投递） |
| `ND_LLM_MODEL` | 智谱模型名（公众号排序用） |
| `ND_FEEDBACK_BASE` | 反馈入口的公网基址（留空 → 反馈按钮不渲染） |
| `ND_HMAC_SECRET` | 反馈链接签名密钥（未配置 → 反馈服务拒绝启动） |

> 注意：配置读取**只认 `ND_*` 前缀**，`SMTP_*` 之类的别名不被识别。
> 生产环境必须同时设置 `ND_FEEDBACK_BASE` 与 `ND_HMAC_SECRET`，否则邮件里不会出现 👍/👎 按钮（属于安全降级，不报错）。

### 4.2 `profile.yaml`（通知侧个性化与行为配置）

| 键 | 说明 |
| --- | --- |
| `campus` | 校区，`thu` / `ruc`（通知链路用） |
| `top_n` | 邮件展示条数 |
| `sections` | 分区顺序 |
| `weights_prior` | 冷启动先验权重（也是在线学习的初值） |
| `keywords_boost` | 关键词侧加速项 |
| `keywords_mute` | 屏蔽词（降权，不是硬删） |
| `source_boost` | 来源级加成 |
| `smtp` / `feedback` / `paths` / `fetch` | SMTP、反馈基址、数据库路径、抓取超时/重试/页数 |

完整可抄的样例见 **`profile.example.yaml`**（内含推荐的文体/讲座/比赛/法学/AI 加权先验）。

公众号文章的排序由 LLM 精选与内置关键词表承担，**不读 `profile.yaml`**。

---

## 5. 手工运行

```bash
# ── 公众号源（当前主链路，无需先 fetch/enrich）──

# 只渲染不发送（干跑：写文件、不落台账、不触发闸门）
.venv/bin/python -m notice_digest.cli send --dry-run

# 真实投递
.venv/bin/python -m notice_digest.cli send

# ── 通知链路（历史源，可选）──

# 抓取列表（分页 + 按 id 去重增量）
.venv/bin/python -m notice_digest.cli fetch --pages 30

# 加工详情（1 req/s 限速，条数越多越慢）
.venv/bin/python -m notice_digest.cli enrich --limit 120

# ── 通用 ──

# 权重与统计概览
.venv/bin/python -m notice_digest.cli stats

# 逐条归因：某条为什么排在前面
.venv/bin/python -m notice_digest.cli explain --top 20
.venv/bin/python -m notice_digest.cli explain --id <条目id>

# 反馈服务（生产由 systemd 托管）
.venv/bin/python -m notice_digest.cli feedback-serve --host 127.0.0.1 --port 8791
```

退出码约定：

| 码 | 含义 |
| --- | --- |
| 0 | 成功（含「可自愈的偏差」与「窗口内无新内容」的空投递） |
| 3 | 致命：重试后仍 5xx / 返回非 JSON / 结构漂移 / 首页为空（会发失败通知邮件） |
| 4 | 发送失败 |

---

## 6. 用 systemd 部署（服务器常驻，Mac 关机也照发）

部署物料在 **`deploy/`**：

| 文件 | 用途 |
| --- | --- |
| `install.sh` | 一键安装（主机从参数/环境变量传入，脚本内不硬编码任何 IP） |
| `notice-digest.service` | 每日流水线（`Type=oneshot`，三段 `ExecStart` 顺序执行） |
| `notice-digest.timer` | 每日 **07:30**（Asia/Shanghai）触发 |
| `notice-digest-failure@.service` | 失败时发提醒邮件的模板单元 |
| `notice-feedback.service` | 反馈服务，只监听 `127.0.0.1:8791` |
| `nginx-notice-digest.conf` | vhost：`location /nd/` 反代 + `limit_req` 限流 |
| `nginx-notice-digest-ratelimit.conf` | http 段的 `limit_req_zone`（nginx 要求该指令只能在 http 上下文） |

> 仓库默认 unit 的前两段 `ExecStart` 是 `fetch` / `enrich`（通知链路）。只用公众号源时删去这两行、只保留 `send` 即可 —— 公众号源不经过这两个步骤。

一键部署：

```bash
# 从本机推送到服务器（主机由参数给出）
./deploy/install.sh --host <user>@<host> --identity ~/.ssh/<key>

# 或已在服务器上（源码已同步后）
sudo ./deploy/install.sh --local
```

安装脚本会：建 `notice-digest` 系统用户 → 建 venv 装依赖 → 生成 `.env`（**已存在则不覆盖，只修正为 0600**）→ 装 systemd 单元 → 改动 nginx 前先备份 → `nginx -t` 通过才 reload。

**安装后必须手工做一次投递验证，再启用定时器**：

```bash
sudo -u notice-digest /opt/notice-digest/.venv/bin/python -m notice_digest.cli send --dry-run
sudo -u notice-digest /opt/notice-digest/.venv/bin/python -m notice_digest.cli send

sudo systemctl enable --now notice-digest.timer
sudo systemctl enable --now notice-feedback.service
systemctl list-timers notice-digest.timer --no-pager
```

运维细节（nginx 反代、备份、故障排查、数据源实测记录、残余风险）见 **`docs/RUNBOOK.md`**。

---

## 7. 通知条目的个性化如何随时间自我调整

（作用于通知链路打分；公众号文章的排序由 LLM 精选承担，不参与下述权重学习。）

三路反馈信号，全部汇入同一个**带上下界的在线 SGD**：

| 信号 | 来源 | 强度 |
| --- | --- | --- |
| 显式正/负 | 邮件内 👍 / 👎 链接（HMAC 签名） | 强 |
| 点击行为 | `/nd/c` 跳转（点开 = 弱正） | 中 |
| 沉默 | 当日进了邮件但没被点开 | 弱负 |

机制要点：

- 每次反馈都对「该条命中的特征」做一次在线更新：分类 one-hot、关键词、来源、紧迫度各占一维；
- 权重**有上下界**，避免连续点赞把某个关键词推到压过一切；
- **每日衰减 0.995**：旧偏好缓慢失效，长期不看的类别会自动回落，防止锁死；
- 学到的新权重写入 SQLite 的 `weights` 表，**不回写 `profile.yaml`**；`profile.yaml` 只在初始化时作为先验。

### 看「现在到底学成了什么样」

```bash
# 全部特征权重与统计（JSON；看 weights 里数值最高的那些键）
.venv/bin/python -m notice_digest.cli stats

# 逐条归因：某条为什么排在前面（会列出每个特征对得分的贡献）
.venv/bin/python -m notice_digest.cli explain --top 20
.venv/bin/python -m notice_digest.cli explain --id <条目id>
```

经验判据：`stats` 里 `cat:*` / `kw:*` 权值明显高于 1.0 的，就是你最近偏好的方向；明显低于 1.0 的说明你在持续忽略它们。

---

## 8. 目录结构

```
notice_digest/     主流水线：gzh_source 公众号源、渲染、投递、反馈服务；fetch/enrich/score 为通知链路（历史源）
weread-push/       微信读书接口工具链：searchbiz 订阅导入、引擎运维 RUNBOOK、gzhmail 独立日报脚本、132 订阅清单
deploy/            systemd 单元、nginx 片段、安装脚本
docs/              RUNBOOK（部署与运维手册，含两个数据源的实测记录）、修复记录
tests/             单元与集成测试（标准库 unittest）
data/              运行期数据（数据库、台账、渲染产物）—— 已被 .gitignore 整体排除
```

---

## 9. 许可

MIT，见 [LICENSE](LICENSE)。
