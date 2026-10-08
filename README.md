# notice-digest

把校园通知聚合站的内容，做**二次加工 + 个性化排序**后，每天定时邮件推送给你（附带 ICS 日历附件）。

站点本身只给「标题 + 摘要 + 一句自由文本时间」；本工具补上中文时间解析、事件抽取、分区归类和按个人偏好排序，并支持用邮件里的 👍/👎 按钮持续调整个性化权重。

---

## 1. 它做什么

流水线五步：

```
fetch → enrich → score → render → send
（抓列表）（抓详情+时间解析）（个性化打分）（HTML+ICS）（SMTP 投递）
```

- **fetch**：分页抓取列表，按 id 去重入库（站点无日期区间参数，增量只能靠分页 + 去重）。
- **enrich**：对未加工的条目逐个请求详情端点，解析中文时间文本 → 结构化时间/地点/截止日期。
- **score**：分类 + 关键词 + 来源 + 紧迫度加权打分，按分区（今天/明天/本周/…）排序。
- **render**：生成 HTML 邮件与 `.ics` 日历附件（含提前提醒）。
- **send**：SMTP 投递，并写投递台账（同日同内容有幂等闸门，防重复发信）。

另有 `feedback-serve`：一个只监听回环地址的反馈服务，接收邮件里的 👍/👎 点击。

---

## 2. 数据来源与抓取礼仪

- **数据源**：`pkuknow.cn`（清华校内通知聚合站）。校区由 URL 路径前缀选择：`/thu/` 清华、`/ruc/` 人大。
- **已知接口约束**（实测，2026-10）：
  - `page_size` 参数被忽略，**固定每页 30 条**；
  - 页数会漂移，**不能用固定页数判断收敛**，只能用「整页已知 / 与上页 id 重复 / 触顶」三个条件；
  - 所有日期区间参数无效；
  - **列表载荷不带时间文本**，时间只能从**详情**端点取。
- **抓取礼仪**：
  - 详情端点按 **1 req/s** 限速，脚本内已实现，请勿调高；
  - 列表只抓增量所需页数（默认 8 页 = 240 条，远超实际日增）；
  - 使用带联系方式的 User-Agent，便于站点在流量异常时联系；
  - 不做并发爆破、不绕过站点访问控制、不抓取与通知无关的接口；
  - **本工具是个人自用聚合器**，请遵守站点条款与 robots 约定；如需大规模使用请先取得站点许可。
- **站点读取门槛**：站点要求先建立访客会话（首次请求返回 403 + `Set-Cookie`）。若遇到持续 403 且响应体为 `READ_SESSION_REQUIRED`，说明客户端没有携带/复用会话 Cookie —— 见 `docs/RUNBOOK.md` 的排查章节。

---

## 3. 安装

依赖极简：**Python 标准库 + `requests`**。

```bash
git clone https://github.com/thu-lawyer/notice-digest.git
cd notice-digest

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
| `ND_FEEDBACK_BASE` | 反馈入口的公网基址（留空 → 反馈按钮不渲染） |
| `ND_HMAC_SECRET` | 反馈链接签名密钥（未配置 → 反馈服务拒绝启动） |

> 注意：配置读取**只认 `ND_*` 前缀**，`SMTP_*` 之类的别名不被识别。
> 生产环境必须同时设置 `ND_FEEDBACK_BASE` 与 `ND_HMAC_SECRET`，否则邮件里不会出现 👍/👎 按钮（属于安全降级，不报错）。

### 4.2 `profile.yaml`（个性化与行为配置）

| 键 | 说明 |
| --- | --- |
| `campus` | 校区，`thu` / `ruc` |
| `top_n` | 邮件展示条数 |
| `sections` | 分区顺序 |
| `weights_prior` | 冷启动先验权重（也是在线学习的初值） |
| `keywords_boost` | 关键词侧加速项 |
| `keywords_mute` | 屏蔽词（降权，不是硬删） |
| `source_boost` | 来源级加成 |
| `smtp` / `feedback` / `paths` / `fetch` | SMTP、反馈基址、数据库路径、抓取超时/重试/页数 |

完整可抄的样例见 **`profile.example.yaml`**（内含推荐的文体/讲座/比赛/法学/AI 加权先验）。

---

## 5. 手工运行

```bash
# 抓取（默认 8 页）
.venv/bin/python -m notice_digest.cli fetch --pages 30

# 加工详情（1 req/s 限速，条数越多越慢）
.venv/bin/python -m notice_digest.cli enrich --limit 120

# 看打分结果（不带 --id 则看 Top N）
.venv/bin/python -m notice_digest.cli explain --top 20
.venv/bin/python -m notice_digest.cli explain --id <条目id>

# 权重与统计概览
.venv/bin/python -m notice_digest.cli stats

# 只渲染不发送（干跑：写文件、不落台账、不触发闸门）
.venv/bin/python -m notice_digest.cli send --dry-run

# 真实投递
.venv/bin/python -m notice_digest.cli send

# 反馈服务（生产由 systemd 托管）
.venv/bin/python -m notice_digest.cli feedback-serve --host 127.0.0.1 --port 8791
```

退出码约定：

| 码 | 含义 |
| --- | --- |
| 0 | 成功（含「可自愈的偏差」） |
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

运维细节（nginx 反代、备份、故障排查、数据源约束、残余风险）见 **`docs/RUNBOOK.md`**。

---

## 7. 个性化如何随时间自我调整

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
notice_digest/     抓取 / 时间解析 / 打分 / 渲染 / 投递 / 反馈服务
deploy/            systemd 单元、nginx 片段、安装脚本
docs/              RUNBOOK（部署与运维手册）、修复记录
tests/             单元与集成测试（标准库 unittest）
data/              运行期数据（数据库、渲染产物）—— 已被 .gitignore 整体排除
```

---

## 9. 许可

MIT，见 [LICENSE](LICENSE)。
