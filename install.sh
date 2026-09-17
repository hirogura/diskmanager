#!/bin/bash
set -e

INSTALL_DIR="/opt/diskmanager"
GIT_REPO="https://github.com/hirogura/diskmanager.git"
PORT=3361
SERVICE_NAME="diskmanager"

info() { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*"; }

echo "=== Disk Manager Installer (GitHub) ==="

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root." >&2
    exit 1
fi

check_existing_repo() {
    if [ "$(git -C "$INSTALL_DIR" remote get-url --all origin)" != "$GIT_REPO" ]; then
        echo "Error: origin must point only to $GIT_REPO. No repository changes were made." >&2
        exit 1
    fi
    if [ "$(git -C "$INSTALL_DIR" symbolic-ref --quiet --short HEAD)" != "main" ]; then
        echo "Error: $INSTALL_DIR must be on the main branch. Switch branches manually." >&2
        exit 1
    fi
    local repo_status
    repo_status="$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=all --ignore-submodules=none)" || exit 1
    if [ -n "$repo_status" ]; then
        echo "Error: $INSTALL_DIR has uncommitted or untracked changes. Save them before updating." >&2
        exit 1
    fi
}

if [ -e "$INSTALL_DIR/.git" ]; then
    check_existing_repo
fi

echo "[1/5] Installing dependencies..."
# OS 判定（Debian/Ubuntu 系は apt、Arch/CachyOS 系は pacman を使用）
# /etc/os-release の ID / ID_LIKE と、利用可能なパッケージマネージャで判定する
OS_ID=""
OS_LIKE=""
if [ -f /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-}"
    OS_LIKE="${ID_LIKE:-}"
fi

install_deps_debian() {
    apt-get update -qq
    apt-get install -y -qq python3 gddrescue smartmontools fdisk git clonezilla partclone rsync parted dosfstools ntfs-3g exfatprogs xfsprogs btrfs-progs e2fsprogs
}

install_deps_arch() {
    # CachyOS / Arch 系のパッケージ名に読み替える
    #   gddrescue -> ddrescue, fdisk -> util-linux (lsblk/blkid/fdisk 同梱), file 追加
    pacman -Sy --noconfirm --needed python ddrescue smartmontools util-linux file git clonezilla partclone rsync parted dosfstools ntfs-3g exfatprogs xfsprogs btrfs-progs e2fsprogs
}

if [ -n "$(echo " $OS_LIKE " | grep -i " arch ")" ] || [ "$OS_ID" = "arch" ] || [ "$OS_ID" = "cachyos" ]; then
    info "Detected Arch-based distribution (${OS_ID}). Using pacman..."
    install_deps_arch
elif command -v pacman >/dev/null 2>&1 && [ ! -x /usr/bin/apt-get ]; then
    # os-release が取れない Arch 系コンテナ等のフォールバック
    info "pacman detected. Using pacman..."
    install_deps_arch
elif [ "$OS_ID" = "debian" ] || [ "$OS_ID" = "ubuntu" ] || command -v apt-get >/dev/null 2>&1; then
    info "Detected Debian-based distribution (${OS_ID}). Using apt-get..."
    install_deps_debian
else
    echo "Error: Unsupported distribution (ID=${OS_ID} LIKE=${OS_LIKE})." >&2
     echo "Please install manually: python3, ddrescue(gddrescue), smartmontools, fdisk(util-linux), git, clonezilla, partclone, rsync, parted" >&2
    exit 1
fi

# systemd サービス用の Python バイナリを解決する（Debian: /usr/bin/python3、CachyOS: /usr/bin/python3 も存在）
PYTHON_BIN="$(command -v python3 || command -v python)"
if [ -z "$PYTHON_BIN" ]; then
    echo "Error: python3 not found after installing dependencies." >&2
    exit 1
fi

echo "[2/5] Downloading Disk Manager from GitHub..."
if [ -e "$INSTALL_DIR/.git" ]; then
    echo "  Existing installation found. Updating from GitHub..."
    check_existing_repo
    git -C "$INSTALL_DIR" fetch --no-tags "$GIT_REPO" refs/heads/main
    git -C "$INSTALL_DIR" merge --ff-only --no-autostash --no-overwrite-ignore FETCH_HEAD
else
    if [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
        echo "Error: $INSTALL_DIR already exists and is not empty." >&2
        echo "Please move it aside or remove it, then re-run this script." >&2
        exit 1
    fi
    git clone --branch main --single-branch "$GIT_REPO" "$INSTALL_DIR"
fi

echo "[3/5] Creating directories..."
mkdir -p "$INSTALL_DIR/logs"

echo "[4/5] Creating systemd service..."
cat > /etc/systemd/system/${SERVICE_NAME}.service << SVCEOF
[Unit]
Description=Disk Manager - Web-based disk management interface
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PYTHON_BIN} ${INSTALL_DIR}/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

echo "[5/5] Enabling and starting service..."
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

# ---- Tailscale serve（Tailnet 内のみ HTTPS 公開） ----
TAILNET_URL=""
if command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
    TAILNET_DNS="$(tailscale status --json 2>/dev/null | "$PYTHON_BIN" -c '
import sys, json
try:
    j = json.load(sys.stdin)
    print(((j.get("Self") or {}).get("DNSName") or "").rstrip("."))
except Exception:
    pass
' || true)"
    if [ -n "$TAILNET_DNS" ]; then
        info "Tailscale serve を設定中 (https://${TAILNET_DNS}:${PORT})..."
        if tailscale serve --bg --https=${PORT} http://127.0.0.1:${PORT} 2>/dev/null; then
            TAILNET_URL="https://${TAILNET_DNS}:${PORT}"
            info "Tailscale serve を設定しました: ${TAILNET_URL}"
            info "公開範囲: Tailnet 内のみ"
        else
            warn "tailscale serve の設定に失敗しました。手動で実行してください:"
            warn "  sudo tailscale serve --bg --https=${PORT} http://127.0.0.1:${PORT}"
        fi
    else
        warn "Tailnet の DNS 名を取得できませんでした。serve 設定は手動で行ってください。"
    fi
else
    info "Tailscale が見つかりません。HTTPS 公開しない場合はこのままで構いません。"
    info "設定する場合: sudo tailscale serve --bg --https=${PORT} http://127.0.0.1:${PORT}"
fi

echo ""
echo "=== Done! ==="
echo "Disk Manager is running at http://localhost:${PORT}"
if [ -n "$TAILNET_URL" ]; then
    echo "HTTPS (Tailnet only): ${TAILNET_URL}"
fi
echo ""
echo "Commands:"
echo "  systemctl status ${SERVICE_NAME}"
echo "  systemctl restart ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
