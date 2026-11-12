#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo 'Run: sudo bash deploy/install.sh'
  exit 1
fi
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
git -C "$repo_dir" rev-parse --verify HEAD >/dev/null
apt-get update
apt-get install -y python3 python3-venv nodejs npm git
getent group capri-di >/dev/null || groupadd --system capri-di
for account in capri di; do
  id "$account" >/dev/null 2>&1 || useradd --system --gid capri-di --home-dir "/var/lib/capri-di/$account" --create-home --shell /usr/sbin/nologin "$account"
  install -d -m 700 -o "$account" -g capri-di "/var/lib/capri-di/$account"
done
install -d -m 2770 -o capri -g capri-di /var/lib/capri-di/shared
install -d -m 755 /opt/capri-di /etc/capri-di
# Install only committed files. No .env, browser profile or auth cache is copied.
git -C "$repo_dir" archive HEAD | tar -x -C /opt/capri-di
python3 -m venv /opt/capri-di/.venv
/opt/capri-di/.venv/bin/pip install --disable-pip-version-check /opt/capri-di
cd /opt/capri-di
npm ci --ignore-scripts
PLAYWRIGHT_BROWSERS_PATH=/opt/capri-di/browsers node_modules/.bin/playwright install --with-deps chromium
chmod -R a+rX /opt/capri-di/browsers
install -d -m 700 -o di -g capri-di /var/lib/capri-di/di/.codex
if [[ ! -e /var/lib/capri-di/di/.codex/config.toml ]]; then
  install -m 600 -o di -g capri-di deploy/codex-config.toml /var/lib/capri-di/di/.codex/config.toml
fi
if [[ ! -e /etc/capri-di/capri.env ]]; then
  install -m 640 -o root -g capri-di .env.example /etc/capri-di/capri.env
  # Only Capri may read its API keys, not the shared group / Di.
  chown root:capri-di /etc/capri-di/capri.env
  chmod 600 /etc/capri-di/capri.env
fi
[[ -e /etc/capri-di/di.env ]] || install -m 600 deploy/di.env.example /etc/capri-di/di.env
[[ -e /etc/capri-di/browser.json ]] || install -m 644 deploy/browser.json /etc/capri-di/browser.json
install -m 644 deploy/capri.service deploy/di.service /etc/systemd/system/
systemctl daemon-reload
echo 'Installed, services NOT started. Fill /etc/capri-di/capri.env, sign Di in, then follow README.md.'
