#!/usr/bin/env bash
# Real OpenRouter model, isolated temporary DB. Never starts Di or sends Telegram.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash deploy/check-routing.sh'; exit 1; }
systemd-run --quiet --wait --pipe --collect \
  --property=User=capri --property=Group=capri-di \
  --property=WorkingDirectory=/opt/capri-di \
  --property=EnvironmentFile=/etc/capri-di/capri.env \
  --property=Environment=HOME=/var/lib/capri-di/capri \
  /opt/capri-di/.venv/bin/python -m capri_di.routing_probe
