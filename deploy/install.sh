#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
  echo 'Run: sudo bash deploy/install.sh'
  exit 1
fi
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
git -C "$repo_dir" rev-parse --verify HEAD >/dev/null
apt-get update
apt-get install -y python3 python3-venv git curl ca-certificates xz-utils
getent group capri-di >/dev/null || groupadd --system capri-di
for account in capri di; do
  id "$account" >/dev/null 2>&1 || useradd --system --gid capri-di --home-dir "/var/lib/capri-di/$account" --create-home --shell /usr/sbin/nologin "$account"
  install -d -m 700 -o "$account" -g capri-di "/var/lib/capri-di/$account"
done
install -d -m 2770 -o capri -g capri-di /var/lib/capri-di/shared
if [[ ! -e /var/lib/capri-di/shared/assistant.db ]]; then
  install -m 660 -o capri -g capri-di /dev/null /var/lib/capri-di/shared/assistant.db
fi
install -d -m 755 /opt/capri-di /etc/capri-di
# Install only committed files. No .env, browser profile or auth cache is copied.
git -C "$repo_dir" archive HEAD | tar -x -C /opt/capri-di
cd /opt/capri-di
# Prepare private configuration before downloads; reruns preserve auth and env.
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
export PATH="/opt/capri-di/node/bin:$PATH"
node_major=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)
if (( node_major < 20 )) || ! command -v npm >/dev/null; then
  case $(uname -m) in
    x86_64) node_arch=x64 ;;
    aarch64) node_arch=arm64 ;;
    *) echo 'Unsupported architecture for managed Node.js'; exit 1 ;;
  esac
  download_dir=$(mktemp -d)
  trap 'rm -rf -- "$download_dir"' EXIT
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt -o "$download_dir/SHASUMS256.txt"
  archive=$(awk -v suffix="linux-$node_arch.tar.xz" '$2 ~ /^node-v22\./ && substr($2,length($2)-length(suffix)+1)==suffix {print $2}' "$download_dir/SHASUMS256.txt")
  [[ $archive =~ ^node-v22\.[0-9]+\.[0-9]+-linux-(x64|arm64)\.tar\.xz$ ]] || { echo 'Invalid Node release manifest'; exit 1; }
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    "https://nodejs.org/dist/latest-v22.x/$archive" -o "$download_dir/$archive"
  (cd "$download_dir"; awk -v name="$archive" '$2==name' SHASUMS256.txt | sha256sum --check --strict -)
  install -d -m 755 /opt/capri-di/node
  tar -xJf "$download_dir/$archive" -C /opt/capri-di/node --strip-components=1 --no-same-owner
  rm -rf -- "$download_dir"
  trap - EXIT
  hash -r
fi
node --version
python3 -m venv /opt/capri-di/.venv
/opt/capri-di/.venv/bin/pip install --disable-pip-version-check /opt/capri-di
npm ci --ignore-scripts
PLAYWRIGHT_BROWSERS_PATH=/opt/capri-di/browsers node_modules/.bin/playwright install --with-deps chromium
chmod -R a+rX /opt/capri-di/browsers
install -m 644 deploy/capri.service deploy/di.service /etc/systemd/system/
systemctl daemon-reload
echo 'Installed, services NOT started. Fill /etc/capri-di/capri.env, sign Di in, then follow README.md.'
