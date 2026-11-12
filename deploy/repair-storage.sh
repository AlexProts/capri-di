#!/usr/bin/env bash
# Repairs only shared database permissions. Does not delete data or read env/auth.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash deploy/repair-storage.sh'; exit 1; }
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
git -C "$repo_dir" rev-parse --verify HEAD >/dev/null
systemctl stop capri di
python3 - <<'PY'
import grp
import os
from pathlib import Path
import pwd
import stat

root = Path('/var/lib/capri-di/shared')
if root.is_symlink() or not root.is_dir():
    raise SystemExit('Expected a real shared database directory')
uid = pwd.getpwnam('capri').pw_uid
gid = grp.getgrnam('capri-di').gr_gid
os.chown(root, uid, gid)
os.chmod(root, 0o2770)
for name in ('assistant.db', 'assistant.db-wal', 'assistant.db-shm', 'assistant.db-journal'):
    path = root / name
    if name != 'assistant.db' and not path.exists() and not path.is_symlink():
        continue
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if name == 'assistant.db' else 0), 0o660)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SystemExit('Expected regular database files')
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o660)
    finally:
        os.close(fd)
print('Shared database permissions repaired; data preserved.')
PY
# Verify actual writes under both service accounts before starting either service.
for account in capri di; do
  sudo -u "$account" python3 - <<'PY'
import sqlite3
with sqlite3.connect('/var/lib/capri-di/shared/assistant.db', timeout=15) as db:
    db.execute('BEGIN IMMEDIATE')
    db.execute('PRAGMA user_version=0')
    db.rollback()
print('Database write check passed.')
PY
done
# Install the committed fix for both import locations used by the services.
git -C "$repo_dir" archive HEAD | tar -x -C /opt/capri-di
/opt/capri-di/.venv/bin/pip install --disable-pip-version-check --no-deps /opt/capri-di
systemctl start di capri
systemctl show capri di -p Id -p ActiveState -p SubState
