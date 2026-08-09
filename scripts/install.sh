#!/usr/bin/env bash
# Install Pi-O-My on Raspberry Pi OS (Bookworm) using uv.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/piomy}"
CONFIG_DIR="${CONFIG_DIR:-/etc/piomy}"
DATA_DIR="${DATA_DIR:-/var/lib/piomy}"
ARCHIVE_DIR="${ARCHIVE_DIR:-${DATA_DIR}/archive}"
SERVICE_USER="${SERVICE_USER:-piomy}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

echo "==> Ensuring uv is installed"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:/root/.local/bin:${PATH}"
fi

echo "==> Creating service user ${SERVICE_USER}"
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home "${DATA_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
usermod -aG video "${SERVICE_USER}" || true

echo "==> Creating directories"
mkdir -p "${INSTALL_ROOT}" "${CONFIG_DIR}" "${ARCHIVE_DIR}" "${DATA_DIR}"
# Copy checkout into install root
rsync -a --delete \
  --exclude '.venv' \
  --exclude '.git' \
  --exclude '__pycache__' \
  "${REPO_ROOT}/" "${INSTALL_ROOT}/src-repo/"

echo "==> Creating uv venv and installing package"
cd "${INSTALL_ROOT}/src-repo"
# system-site-packages: use apt python3-picamera2
# --clear: replace existing venv on redeploy without prompting
uv venv --clear --system-site-packages "${INSTALL_ROOT}/venv"
uv pip install --python "${INSTALL_ROOT}/venv/bin/python" -e .
if ! "${INSTALL_ROOT}/venv/bin/python" -c "import picamera2" 2>/dev/null; then
  echo "NOTE: picamera2 not importable. Install: sudo apt install -y python3-picamera2"
fi

if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  echo "==> Writing default config"
  cp "${REPO_ROOT}/config.example.yaml" "${CONFIG_DIR}/config.yaml"
  # Point archive at DATA_DIR
  "${INSTALL_ROOT}/venv/bin/python" - <<PY
from pathlib import Path
import yaml
p = Path("${CONFIG_DIR}/config.yaml")
data = yaml.safe_load(p.read_text()) or {}
data.setdefault("storage", {})["archive_dir"] = "${ARCHIVE_DIR}"
from piomy.auth import hash_password
data.setdefault("web", {})["password_hash"] = hash_password("changeme")
p.write_text(yaml.safe_dump(data, sort_keys=False))
print("Default web password: changeme  (change it in the UI)")
PY
fi

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}" "${ARCHIVE_DIR}"
chown root:"${SERVICE_USER}" "${CONFIG_DIR}"
chmod 775 "${CONFIG_DIR}"
chmod 660 "${CONFIG_DIR}/config.yaml"
chown root:"${SERVICE_USER}" "${CONFIG_DIR}/config.yaml"

echo "==> Installing systemd units"
cp "${REPO_ROOT}/systemd/piomy-capture.service" /etc/systemd/system/
cp "${REPO_ROOT}/systemd/piomy-web.service" /etc/systemd/system/
cp "${REPO_ROOT}/systemd/piomy-sync.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable piomy-capture.service piomy-web.service piomy-sync.service
systemctl restart piomy-capture.service piomy-web.service piomy-sync.service

BOOT_CFG=""
for candidate in /boot/firmware/config.txt /boot/config.txt; do
  if [[ -f "${candidate}" ]]; then
    BOOT_CFG="${candidate}"
    break
  fi
done

if [[ -n "${BOOT_CFG}" ]]; then
  if ! grep -q '^disable_camera_led=1' "${BOOT_CFG}"; then
    echo "==> Optional: to reduce red glare behind glass, add to ${BOOT_CFG} and reboot:"
    echo "    disable_camera_led=1"
    echo "    (Check local rules first; some places require a visible camera indicator.)"
  fi
fi

echo "==> Done"
echo "UI: http://$(hostname -I | awk '{print $1}'):8080  (user any, password changeme unless changed)"
echo "Mount your SSD at ${ARCHIVE_DIR} (or change storage.archive_dir in Settings)."
echo "For Samba sync: apt install rclone; put password in ${CONFIG_DIR}/smb.cred (chmod 600); enable in Settings."
