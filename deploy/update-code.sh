#!/usr/bin/env bash
# Update code without changing env/auth or interrupting Di's queued/running work.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash deploy/update-code.sh'; exit 1; }
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
git -C "$repo_dir" rev-parse --verify HEAD >/dev/null
systemctl is-active --quiet capri && capri_active=true || capri_active=false
systemctl is-active --quiet di && di_active=true || di_active=false
restore() {
  [[ $di_active == false ]] || systemctl start di
  [[ $capri_active == false ]] || systemctl start capri
}
trap restore EXIT
# Stop the producer first; after an idle queue check, no new work can appear.
systemctl stop capri
busy=$(python3 - <<'PY'
import sqlite3
with sqlite3.connect('file:/var/lib/capri-di/shared/assistant.db?mode=ro', uri=True) as db:
    print(db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0])
PY
)
if (( busy > 0 )); then
  echo 'Di has queued or running work. No code changed; wait for completion and rerun.'
  exit 1
fi
systemctl stop di
git -C "$repo_dir" archive HEAD | tar -x -C /opt/capri-di
/opt/capri-di/.venv/bin/pip install --disable-pip-version-check --no-deps /opt/capri-di
echo 'Code updated. Previous service states are being restored; env and browser sessions unchanged.'
