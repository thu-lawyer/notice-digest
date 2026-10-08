#!/usr/bin/env bash
# notice-digest 部署脚本
#
# 两种用法
#   A. 从本机推送到服务器（推荐）：
#        ./deploy/install.sh --host <user>@<host> [--dir /opt/notice-digest] [--identity ~/.ssh/<key>]
#      主机也可用环境变量：ND_DEPLOY_HOST=<user>@<host>
#      —— 脚本会 rsync 本仓库到目标机，再在目标机上执行自身（--local）完成安装。
#
#   B. 已在目标机上（从别处同步好源码后）：
#        sudo ./deploy/install.sh --local [--dir /opt/notice-digest]
#
# 设计约束
#   * 主机地址只从参数或环境变量来，**脚本内不硬编码任何 IP**。
#   * 幂等：重复执行安全；已存在的 .env 永不覆盖（只修正权限）。
#   * 改动 nginx 前先备份既有文件；nginx -t 通过才 reload。
#   * 不触碰同机其它租户（aidigest / usclose / personal-site / artalk / lawq）。
#   * 不在本脚本里写任何密钥；SMTP 口令由使用者手工写入 .env。

set -euo pipefail

APP_DIR="/opt/notice-digest"
SERVICE_USER="notice-digest"
NGINX_SITE="/etc/nginx/sites-enabled/notice-digest.conf"
NGINX_ZONE="/etc/nginx/conf.d/notice-digest-ratelimit.conf"
HOST="${ND_DEPLOY_HOST:-}"
IDENTITY=""
MODE=""
STAMP="$(date +%Y%m%d-%H%M%S)"

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install][warn] %s\n' "$*" >&2; }
die()  { printf '[install][error] %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --host)     HOST="${2:-}"; shift 2 ;;
    --host=*)   HOST="${1#*=}"; shift ;;
    --dir)      APP_DIR="${2:-}"; shift 2 ;;
    --dir=*)    APP_DIR="${1#*=}"; shift ;;
    --identity) IDENTITY="${2:-}"; shift 2 ;;
    --identity=*) IDENTITY="${1#*=}"; shift ;;
    --local)    MODE="local"; shift ;;
    -h|--help)  usage 0 ;;
    *)          die "未知参数：$1（用 --help 看用法）" ;;
  esac
done

[ -n "$APP_DIR" ] || die "--dir 不能为空"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------- 推送模式
if [ "$MODE" != "local" ]; then
  [ -n "$HOST" ] || die "缺少目标主机：--host <user>@<host> 或环境变量 ND_DEPLOY_HOST"
  case "$HOST" in
    *@*) : ;;
    *) die "--host 需要形如 <user>@<host>（当前：$HOST）" ;;
  esac

  SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
  [ -n "$IDENTITY" ] && SSH_OPTS+=(-i "$IDENTITY" -o IdentitiesOnly=yes)

  log "同步源码 $SRC_DIR → $HOST:$APP_DIR"
  ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p '$APP_DIR'"

  # 排除运行期数据、虚拟环境与 git 元数据
  rsync -az --delete \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude 'data/' \
    --exclude '.env' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    -e "ssh ${SSH_OPTS[*]}" \
    "$SRC_DIR/" "$HOST:$APP_DIR/"

  log "在目标机执行安装（--local）"
  ssh "${SSH_OPTS[@]}" "$HOST" "bash '$APP_DIR/deploy/install.sh' --local --dir '$APP_DIR'"
  exit 0
fi

# ---------------------------------------------------------------- 本地模式
[ "$(id -u)" = "0" ] || die "--local 需要 root（sudo $0 --local）"

command -v python3 >/dev/null || die "缺少 python3"
python3 -c 'import venv' 2>/dev/null || die "python3 缺少 venv 模块（apt install python3-venv）"

log "目标目录：$APP_DIR"

# 1) 系统用户
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  log "创建系统用户 $SERVICE_USER"
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" \
    || die "创建用户失败"
else
  log "系统用户 $SERVICE_USER 已存在，跳过"
fi

# 2) 目录与属主
mkdir -p "$APP_DIR/data"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 755 "$APP_DIR"
chmod 750 "$APP_DIR/data"

# 3) venv + 依赖
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  log "创建 venv：$APP_DIR/.venv"
  python3 -m venv "$APP_DIR/.venv" || die "创建 venv 失败"
fi
log "安装依赖（requirements.txt）"
"$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
if [ -f "$APP_DIR/requirements.txt" ]; then
  "$APP_DIR/.venv/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt" \
    || die "依赖安装失败"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.venv"

# 4) .env —— 存在就不动，只修正权限；不存在则从示例复制并提醒填写
if [ ! -f "$APP_DIR/.env" ]; then
  if [ -f "$APP_DIR/.env.example" ]; then
    install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" "$APP_DIR/.env.example" "$APP_DIR/.env"
    warn "已从 .env.example 生成 $APP_DIR/.env（占位值）——**必须**填入真实 SMTP 账号/授权码后再运行"
  else
    warn "缺少 $APP_DIR/.env 与 .env.example，请手工创建 .env（权限 0600）"
  fi
else
  log ".env 已存在，保留不动（只修正权限）"
fi
[ -f "$APP_DIR/.env" ] && chmod 0600 "$APP_DIR/.env" && chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"

# 5) systemd 单元
log "安装 systemd 单元 → /etc/systemd/system/"
for unit in notice-digest.service notice-digest.timer \
            notice-digest-failure@.service notice-feedback.service; do
  src="$APP_DIR/deploy/$unit"
  [ -f "$src" ] || die "缺少单元文件：$src"
  install -m 0644 "$src" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
log "systemd 单元已安装（尚未 enable——先手工跑一次确认后再启用 timer）"

# 6) nginx（新 vhost + http 段限流区，互不改动既有配置）
if command -v nginx >/dev/null 2>&1; then
  need_reload=0

  for pair in "$APP_DIR/deploy/nginx-notice-digest-ratelimit.conf:$NGINX_ZONE" \
              "$APP_DIR/deploy/nginx-notice-digest.conf:$NGINX_SITE"; do
    src="${pair%%:*}"; dst="${pair##*:}"
    [ -f "$src" ] || die "缺少 nginx 片段：$src"
    if [ -f "$dst" ] && ! cmp -s "$src" "$dst"; then
      cp -p "$dst" "$dst.bak-$STAMP"
      log "已备份 $dst → $dst.bak-$STAMP"
    fi
    install -m 0644 "$src" "$dst"
    need_reload=1
    log "写入 nginx 配置：$dst"
  done

  log "校验 nginx 配置（nginx -t）"
  if nginx -t; then
    if [ "$need_reload" = "1" ]; then
      log "reload nginx"
      systemctl reload nginx
    fi
  else
    die "nginx -t 未通过：已写入的片段保留在 $NGINX_SITE / $NGINX_ZONE，请修好后手工 reload；既有配置可在 *.bak-$STAMP 找到"
  fi
else
  warn "未检测到 nginx，跳过反馈入口配置"
fi

cat <<EOF

[install] 安装完成。后续步骤（务必按顺序，且**先手工跑一次再启用定时器**）：

  1) 编辑密钥（仅服务器本地，永不进 git）：
       sudo -e $APP_DIR/.env
     必填项见 $APP_DIR/.env.example 的注释；确认真实 SMTP 账号/授权码已填入。
       sudo chmod 0600 $APP_DIR/.env && sudo chown $SERVICE_USER:$SERVICE_USER $APP_DIR/.env

  2) 冒烟自检（以服务用户身份，只写文件不发信）：
       sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python -m notice_digest.cli send --dry-run

  3) 手工真实投递一次：
       sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python -m notice_digest.cli send

  4) 确认无误后启用定时器与反馈服务：
       sudo systemctl enable --now notice-digest.timer
       sudo systemctl enable --now notice-feedback.service
       systemctl list-timers notice-digest.timer --no-pager

详见 $APP_DIR/docs/RUNBOOK.md 与 README.md。
EOF
