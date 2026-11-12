#!/usr/bin/env bash
# Enable automatic action review (not full access) and the already-authorized Time source.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash deploy/repair-browser.sh'; exit 1; }
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
git -C "$repo_dir" rev-parse --verify HEAD >/dev/null
PATH=/opt/capri-di/node/bin:/opt/capri-di/node_modules/.bin:/usr/local/bin:/usr/bin:/bin
export PATH
codex exec --help | grep -q -- '--approve-for-me'
systemctl stop capri di
python3 - "$repo_dir" <<'PY'
import os
from pathlib import Path
import re
import sys

def set_key(path, key, value, separator):
    # Read only inside this process; never print env/config contents.
    text = path.read_text()
    pattern = re.compile(r'^\s*' + re.escape(key) + r'\s*=.*$')
    replacement = key + separator + value
    lines = []
    for line in text.splitlines():
        if not pattern.match(line):
            lines.append(line)
    text = replacement + '\n' + '\n'.join(lines) + '\n'
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, 'w') as file:
        file.write(text)

config = Path('/var/lib/capri-di/di/.codex/config.toml')
set_key(config, 'approval_policy', '"on-request"', ' = ')
set_key(config, 'approvals_reviewer', '"auto_review"', ' = ')
runtime_env = Path('/etc/capri-di/capri.env')
set_key(runtime_env, 'TIME_ENABLED', 'true', '=')
os.chmod(runtime_env, 0o600)
source_env = Path(sys.argv[1]) / '.env'
if source_env.is_file() and not source_env.is_symlink():
    set_key(source_env, 'TIME_ENABLED', 'true', '=')
print('Automatic review configured; workspace restrictions retained. TIME_ENABLED=true.')
PY
git -C "$repo_dir" archive HEAD | tar -x -C /opt/capri-di
/opt/capri-di/.venv/bin/pip install --disable-pip-version-check --no-deps /opt/capri-di
systemctl start di capri
systemctl show capri di -p Id -p ActiveState -p SubState
echo 'No jobs replayed. Request a new /digest and /time to verify read-only collection.'
