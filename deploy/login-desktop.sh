#!/usr/bin/env bash
# Run manually on the server. No passwords, cookies or codes are printed.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo'; exit 1; }
source_name=${1:-lms}
[[ $source_name == lms || $source_name == time ]] || { echo 'Choose lms or time'; exit 1; }
apt-get update
apt-get install -y tigervnc-standalone-server tigervnc-tools openbox xauth
di_home=/var/lib/capri-di/di
passfile=$di_home/vnc-passwd
authfile=$di_home/vnc-xauthority
systemctl is-active --quiet di && was_active=true || was_active=false
systemctl stop di
if [[ ! -f $passfile ]]; then
  echo 'Set a temporary VNC password (enter here, never in Telegram):'
  sudo -u di tigervncpasswd "$passfile"
fi
if [[ -S /tmp/.X11-unix/X1 ]]; then
  echo 'Display :1 already exists. No session changed; close it before retrying.'
  [[ $was_active == false ]] || systemctl start di
  exit 1
fi
cleanup() {
  [[ -z ${desktop_pid:-} ]] || kill "$desktop_pid" 2>/dev/null || true
  [[ -z ${vnc_pid:-} ]] || kill "$vnc_pid" 2>/dev/null || true
  [[ $was_active == false ]] || systemctl start di
}
trap cleanup EXIT
install -m 600 -o di -g capri-di /dev/null "$authfile"
cookie=$(mcookie)
sudo -u di xauth -f "$authfile" add :1 . "$cookie"
unset cookie
sudo -u di Xtigervnc :1 -rfbport 5901 -localhost yes -SecurityTypes VncAuth \
  -rfbauth "$passfile" -auth "$authfile" -nolisten tcp -geometry 1280x900 -depth 24 \
  >"$di_home/vnc.log" 2>&1 &
vnc_pid=$!
for _ in {1..50}; do
  [[ ! -S /tmp/.X11-unix/X1 ]] || break
  sleep 0.1
done
[[ -S /tmp/.X11-unix/X1 ]] || { echo 'VNC startup failed'; exit 1; }
sudo -u di env HOME="$di_home" DISPLAY=:1 XAUTHORITY="$authfile" openbox-session \
  >"$di_home/desktop.log" 2>&1 &
desktop_pid=$!
echo 'On your computer: ssh -N -L 5901:127.0.0.1:5901 YOUR_USER@YOUR_SERVER_IP'
echo 'Then open vnc://127.0.0.1:5901 and sign in. Close the browser window when finished.'
sudo -u di env HOME="$di_home" DISPLAY=:1 XAUTHORITY="$authfile" \
  PLAYWRIGHT_BROWSERS_PATH=/opt/capri-di/browsers \
  node /opt/capri-di/deploy/browser-login.mjs "$source_name"
