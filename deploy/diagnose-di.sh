#!/usr/bin/env bash
# Reports selected facts only. Does not read auth.json, print env, or replay the job.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash deploy/diagnose-di.sh JOB_ID'; exit 1; }
job_id=${1:-}
[[ $job_id =~ ^[a-f0-9]{12}$ ]] || { echo 'Expected a 12-character job ID'; exit 1; }
sudo -u di env HOME=/var/lib/capri-di/di CODEX_HOME=/var/lib/capri-di/di/.codex \
  PATH=/opt/capri-di/node/bin:/opt/capri-di/node_modules/.bin:/usr/local/bin:/usr/bin:/bin \
  PLAYWRIGHT_BROWSERS_PATH=/opt/capri-di/browsers \
  /opt/capri-di/.venv/bin/python - "$job_id" <<'PY'
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urlsplit, urlunsplit
from capri_di.worker import validate_result

def mask(text):
    text = re.sub(r'(?:sk-[A-Za-z0-9_-]+|\b\d{6,}:[A-Za-z0-9_-]{20,}|\beyJ[A-Za-z0-9_.-]{20,})', '[secret]', text)
    text = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [secret]', text)
    text = re.sub(r'(?i)((?:access_token|refresh_token|api_key|authorization)\s*[=:]\s*)\S+', r'\1[secret]', text)
    def safe_url(match):
        try:
            value = urlsplit(match[0])
            return urlunsplit((value.scheme, value.hostname or '', value.path, '', ''))
        except ValueError:
            return '[url]'
    return re.sub(r'https?://[^\s<>"\x27]+', safe_url, text)[:3500]

job_id = sys.argv[1]
db = sqlite3.connect('file:/var/lib/capri-di/shared/assistant.db?mode=ro', uri=True)
db.row_factory = sqlite3.Row
job = db.execute('SELECT id,kind,state,request,result,created,updated FROM jobs WHERE id=?', (job_id,)).fetchone()
db.close()
report = {'job_found': job is not None}
if job:
    report['job'] = {'id': job['id'], 'kind': job['kind'], 'state': job['state'],
                     'request': mask(job['request'])[:1000], 'elapsed_seconds': round(job['updated']-job['created'], 2)}
    if job['result']:
        result = json.loads(job['result'])
        report['job']['summary'] = mask(result.get('summary', ''))
    output = Path('/var/lib/capri-di/di/jobs') / job_id / 'result.json'
    report['result_file_exists'] = output.is_file()
    if output.is_file() and output.stat().st_size < 2*1024*1024:
        try:
            validate_result(json.loads(output.read_text()), job['kind'])
            report['result_schema_valid'] = True
        except (OSError, ValueError, KeyError, TypeError) as error:
            report['result_schema_valid'] = False
            report['validation_error'] = mask(str(error))
config = tomllib.loads(Path('/var/lib/capri-di/di/.codex/config.toml').read_text())
report['approval_policy'] = config.get('approval_policy', 'default')
report['approvals_reviewer'] = config.get('approvals_reviewer', 'default')
report['browser_profile_exists'] = Path('/var/lib/capri-di/di/browser-profile').is_dir()
login = subprocess.run(['codex', 'login', 'status'], capture_output=True, text=True, timeout=20)
report['chatgpt_login_confirmed'] = login.returncode == 0 and 'chatgpt' in (login.stdout+login.stderr).lower()
# Independent tiny smoke test: no site access, tools, homework, or test submissions.
with tempfile.TemporaryDirectory(prefix='di-probe-') as directory:
    output = Path(directory) / 'reply.txt'
    try:
        probe = subprocess.run(['codex', 'exec', '--approve-for-me', '--sandbox', 'workspace-write',
                                '--skip-git-repo-check', '--cd', directory, '--output-last-message', str(output), '-'],
                               input='Do not call tools, browse, or modify files. Reply with exactly OK.',
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=90)
        report['probe_exit_code'] = probe.returncode
        report['probe_reply_ok'] = output.is_file() and output.read_text().strip() == 'OK'
        report['probe_diagnostic'] = mask(probe.stderr)
    except subprocess.TimeoutExpired:
        report['probe_timeout'] = True
print(json.dumps(report, ensure_ascii=False, indent=2))
PY
