#!/usr/bin/env bash
# Real OpenRouter model, isolated temporary DB. Never starts Di or sends Telegram.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash deploy/check-routing.sh'; exit 1; }
[[ $# -le 1 ]] || { echo 'Usage: check-routing.sh [model-id]'; exit 1; }
model_override=()
if [[ $# -eq 1 ]]; then
  [[ $1 =~ ^[a-z0-9][a-z0-9._/-]{1,100}$ ]] || { echo 'Invalid model ID'; exit 1; }
  model_override=("--property=Environment=OPENROUTER_ROUTING_MODEL=$1")
fi
systemd-run --quiet --wait --pipe --collect \
  --property=User=capri --property=Group=capri-di \
  --property=WorkingDirectory=/opt/capri-di \
  --property=EnvironmentFile=/etc/capri-di/capri.env \
  --property=Environment=HOME=/var/lib/capri-di/capri \
  "${model_override[@]}" \
  /opt/capri-di/.venv/bin/python -m capri_di.routing_probe
