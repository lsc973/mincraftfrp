#!/usr/bin/env bash
#
# 在一台全新的 Ubuntu/Debian 服务器上把 lanlink 中继装好。
#
#   sudo bash deploy_relay.sh --token 你的口令
#
# 做完的事：建专用账号、放代码、写口令文件（600 权限）、装 systemd 服务、
# 开本机防火墙、启动，最后验证一遍。
#
# **脚本做不到的那一步**：云服务商控制台里的安全列表/安全组。Oracle、
# 腾讯云、阿里云默认都把所有端口关着，不在控制台放行的话，本机怎么配都没用。
# 脚本跑完会提醒你。
#
# 重复跑是安全的：已经有了的东西会跳过，不会把配置搞乱。

set -euo pipefail

PORT=9000
BIND=0.0.0.0
INSTALL_DIR=/opt/lanlink
CONFIG_DIR=/etc/lanlink
TOKEN_FILE="$CONFIG_DIR/token"
SERVICE=lanlink-relay
USER_NAME=lanlink
REPO=https://github.com/lsc973/mincraftfrp.git

die() { echo "错误：$*" >&2; exit 1; }
info() { echo "  $*"; }
step() { echo; echo "=== $* ==="; }

# ---------------------------------------------------------------- 参数

TOKEN=""
FORCE_TOKEN=no

# 取值之前先确认后面真的跟了一个值。
# 少了这一步的话，`--token` 写在最后会让 `shift 2` 失败，而失败之后 $# 不变，
# 于是 while 循环原地转圈 —— 表现为脚本卡死，还不报错，非常难查。
need_value() {
    [ -n "${2:-}" ] || die "$1 后面要跟一个值（用 --help 看用法）"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --token)        need_value "$1" "${2:-}"; TOKEN="$2"; shift 2 ;;
        --token=*)      TOKEN="${1#*=}"; shift ;;
        --force-token)  FORCE_TOKEN=yes; shift ;;
        --port)         need_value "$1" "${2:-}"; PORT="$2"; shift 2 ;;
        --port=*)       PORT="${1#*=}"; shift ;;
        --repo)         need_value "$1" "${2:-}"; REPO="$2"; shift 2 ;;
        --repo=*)       REPO="${1#*=}"; shift ;;
        -h|--help)
            # 只打文件开头那段注释，不要一路打到代码里
            awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
            exit 0 ;;
        *)              die "不认识的参数：$1（用 --help 看用法）" ;;
    esac
done

# ---------------------------------------------------------------- 前置检查

step "检查环境"

[ "$(uname -s)" = "Linux" ] || die "这个脚本只能在 Linux 上跑"
[ "$(id -u)" = "0" ] || die "要用 root 跑：sudo bash $0 --token 你的口令"

command -v python3 >/dev/null 2>&1 || die "没找到 python3。
  Ubuntu/Debian 上装一下：apt update && apt install -y python3"

PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 8) else 0)')
[ "$PY_OK" = "1" ] || die "Python 版本太低（$(python3 -V)），需要 3.8 以上"

# 用实际路径而不是写死 /usr/bin/python3 —— 装在不同地方的机器多的是
# （pyenv、源码编译、/usr/local/bin……）。systemd 里路径写错的话，
# 报的是 "No such file or directory"，很难一眼看出是解释器找不到。
PYTHON=$(command -v python3)
info "Python：$(python3 -V)   [$PYTHON]"

case "$PORT" in
    ''|*[!0-9]*) die "端口得是数字，收到：$PORT" ;;
esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "端口超出范围：$PORT"

if [ -z "$TOKEN" ]; then
    # 没给口令就自动生成一个 —— 空口令的中继等于公开的流量中转站，
    # 谁都能拿来用。宁可替你生成一个，也别留个口子。
    TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')
    echo
    echo "  没给 --token，自动生成了一个："
    echo
    echo "      $TOKEN"
    echo
    echo "  **记下来** —— 打包给对面的 exe 时要用它（--relay-token）。"
    echo "  它同时存在 $TOKEN_FILE 里，忘了可以 cat 一下。"
fi

# ---------------------------------------------------------------- 账号与目录

step "建账号和目录"

if id "$USER_NAME" >/dev/null 2>&1; then
    info "账号 $USER_NAME 已存在，跳过"
else
    useradd --system --no-create-home --shell /usr/sbin/nologin "$USER_NAME"
    info "已建系统账号 $USER_NAME（不能登录，只用来跑服务）"
fi

install -d -o "$USER_NAME" -g "$USER_NAME" "$INSTALL_DIR" "$CONFIG_DIR"

# ---------------------------------------------------------------- 代码

step "放代码"

# 如果这个脚本就在仓库里（从仓库里直接跑），就用现成的，不用联网 clone
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [ -f "$HERE/../lanlink/__main__.py" ]; then
    SRC=$(cd "$HERE/.." && pwd)
    info "用当前目录里的代码：$SRC"
else
    SRC=$(mktemp -d)
    command -v git >/dev/null 2>&1 || die "没找到 git，装一下：apt install -y git"
    info "从 $REPO 拉代码…"
    git clone --depth 1 "$REPO" "$SRC/repo" >/dev/null 2>&1 || die "拉代码失败，检查网络"
    SRC="$SRC/repo"
fi

# 只拷跑得起来需要的东西，测试和打包脚本留在本地
rm -rf "$INSTALL_DIR/lanlink"
cp -r "$SRC/lanlink" "$INSTALL_DIR/"
chown -R "$USER_NAME:$USER_NAME" "$INSTALL_DIR"
info "代码已放到 $INSTALL_DIR"
info "（纯标准库，不需要 pip install 任何东西）"

# ---------------------------------------------------------------- 口令

step "写口令文件"

if [ -f "$TOKEN_FILE" ] && [ "$FORCE_TOKEN" = "no" ]; then
    # 已经有口令就不覆盖 —— 覆盖了对面手上那个 exe 里的口令就对不上，
    # 他会一直连不上，而两边都看不出问题在哪。
    EXISTING=$(cat "$TOKEN_FILE")
    if [ "$EXISTING" != "$TOKEN" ]; then
        info "已经有口令文件了，**保持原样不动**（免得对面手上的 exe 失效）"
        info "确实要换的话：加 --force-token 重跑，记得重新打包给对面"
        TOKEN="$EXISTING"
    else
        info "口令文件已存在且一致，跳过"
    fi
else
    printf '%s' "$TOKEN" > "$TOKEN_FILE"
    info "已写入 $TOKEN_FILE（权限 600）"
fi
chown "$USER_NAME:$USER_NAME" "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

# ---------------------------------------------------------------- systemd

step "装 systemd 服务"

UNIT=/etc/systemd/system/$SERVICE.service
cat > "$UNIT" <<UNIT_EOF
# 由 tools/deploy_relay.sh 生成
[Unit]
Description=lanlink relay server（局域网联机工具的公网中继）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
WorkingDirectory=$INSTALL_DIR

# 口令走文件而不是命令行参数 —— 命令行参数会出现在 ps 输出里，谁都看得见
ExecStart=$PYTHON -m lanlink relay --bind $BIND --port $PORT --token-file $TOKEN_FILE --log-level INFO
Environment=PYTHONUNBUFFERED=1

KillSignal=SIGTERM
TimeoutStopSec=10
Restart=always
RestartSec=5

# 退出码 2 = 端口起不来这类配置错误，重试一万次也没用，只会把日志刷满
RestartPreventExitStatus=2

# 每客户端一条连接，房间一多默认的 1024 就不够用
LimitNOFILE=65536

# 中继只搬字节、不写文件，能锁多紧锁多紧
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6
RestrictNamespaces=true
RestrictRealtime=true
LockPersonality=true
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
info "单元文件：$UNIT"

# ---------------------------------------------------------------- 防火墙

step "开本机防火墙"

# 云服务商控制台那一层脚本碰不到，这里只管服务器自己这层
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$PORT"/tcp >/dev/null 2>&1 && info "ufw：已放行 $PORT/tcp"
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="$PORT"/tcp >/dev/null 2>&1
    firewall-cmd --reload >/dev/null 2>&1
    info "firewalld：已放行 $PORT/tcp"
elif command -v iptables >/dev/null 2>&1; then
    # Oracle 的 Ubuntu 镜像默认就带着 iptables 规则，光改安全列表是不够的
    if iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; then
        info "iptables：已经放行过了"
    else
        iptables -I INPUT -p tcp --dport "$PORT" -j ACCEPT
        info "iptables：已放行 $PORT/tcp"
        if command -v netfilter-persistent >/dev/null 2>&1; then
            netfilter-persistent save >/dev/null 2>&1 && info "已保存规则（重启后仍生效）"
        else
            info "注意：没装 netfilter-persistent，重启后这条规则会丢"
            info "  装一下：apt install -y iptables-persistent"
        fi
    fi
else
    info "没找到认识的防火墙工具，跳过（如果连不上再回来查）"
fi

# ---------------------------------------------------------------- 启动

step "启动"

systemctl restart "$SERVICE"
sleep 3

if systemctl is-active --quiet "$SERVICE"; then
    info "服务在跑 ✅"
else
    echo
    echo "  启动失败，最近的日志："
    echo
    journalctl -u "$SERVICE" -n 25 --no-pager | sed 's/^/    /'
    echo
    die "中继没起来，先看上面的日志"
fi

# 验证端口真的在监听
if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
        info "端口 $PORT 在监听 ✅"
    else
        die "服务说在跑，但端口 $PORT 没在监听"
    fi
fi

# ---------------------------------------------------------------- 收尾

PUBLIC_IP=$(timeout 8 python3 -c "
import http.client
try:
    c = http.client.HTTPSConnection('api.ipify.org', 443, timeout=6)
    c.request('GET', '/')
    print(c.getresponse().read().decode())
except Exception:
    print('')
" 2>/dev/null || true)

echo
echo "=============================================================="
echo "  中继装好了"
echo "=============================================================="
echo
echo "  地址：${PUBLIC_IP:-<这台机器的公网IP>}:$PORT"
echo "  口令：$TOKEN"
echo
echo "--------------------------------------------------------------"
echo "  ⚠️  还差一步：去云服务商控制台放行端口"
echo "--------------------------------------------------------------"
echo
echo "  脚本只能改这台机器自己的防火墙。云服务商那边还有一层，"
echo "  不去控制台放行的话，外面永远连不上 —— 这是最常见的坑。"
echo
echo "  Oracle Cloud：实例 → 主 VNIC → 子网 → 安全列表 → 添加入站规则"
echo "                源 CIDR 0.0.0.0/0，协议 TCP，端口 $PORT"
echo "  腾讯云/阿里云：安全组里加一条同样的规则"
echo
echo "--------------------------------------------------------------"
echo "  弄好之后验证"
echo "--------------------------------------------------------------"
echo
echo "  在你自己的电脑上跑（不是在这台服务器上）："
echo
echo "      lanlink-cli.exe list --relay ${PUBLIC_IP:-<公网IP>}:$PORT"
echo
echo "  能列出「中继 xxx 上的房间：（没有）」就说明通了。"
echo
echo "--------------------------------------------------------------"
echo "  然后打给对面的包"
echo "--------------------------------------------------------------"
echo
echo "      python packaging/build.py --mode gui \\"
echo "        --server-relay ${PUBLIC_IP:-<公网IP>}:$PORT \\"
echo "        --relay-token $TOKEN \\"
echo "        --label \"给小明的\""
echo "      python tools/make_release.py --label \"给小明的\""
echo
echo "--------------------------------------------------------------"
echo "  日常维护"
echo "--------------------------------------------------------------"
echo
echo "      systemctl status $SERVICE      # 看状态"
echo "      journalctl -u $SERVICE -f      # 看实时日志"
echo "      systemctl restart $SERVICE     # 重启"
echo
echo "  服务已经设成开机自启，服务器重启后不用管。"
echo
