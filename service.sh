#!/bin/bash
# Manage the watchdog in the current macOS login session.
set -euo pipefail
umask 077

base="$(cd -- "$(dirname -- "$0")" && pwd)"
label=local.codex-health.grafana
domain="gui/$(id -u)"
agent="$HOME/Library/LaunchAgents/$label.plist"
runtime="${GRAFANA_RUNTIME_DIR:-$HOME/Library/Application Support/Codex Health Grafana}"
port="${GRAFANA_PORT:-3308}"

case "${1:-status}" in
  start|prepare)
    if [[ "$1" == start ]] && launchctl print "$domain/$label" >/dev/null 2>&1; then
      printf 'Already loaded · http://127.0.0.1:%s/d/codex-health\n' "$port"
      exit 0
    fi
    mkdir -p "$runtime/app"
    runtime="$(cd -- "$runtime" && pwd)"
    for asset in run.sh collect.py dashboard.json; do
      install -m 600 "$base/$asset" "$runtime/app/$asset"
    done
    if [[ -f "$base/codex-health.sqlite" && ! -f "$runtime/app/codex-health.sqlite" ]]; then
      install -m 600 "$base/codex-health.sqlite" "$runtime/app/codex-health.sqlite"
    fi
    python3 - "$runtime" "$label" <<'PY'
import os
import plistlib
import sys
from pathlib import Path

runtime = Path(sys.argv[1])
base = runtime / 'app'
label = sys.argv[2]
env = {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
       'GRAFANA_RUNTIME_DIR': str(runtime), 'PYTHONUNBUFFERED': '1'}
for key in ('GRAFANA_HOME', 'GRAFANA_PORT', 'CODEX_HOME', 'CODEX_HEALTH_DAYS'):
    if key in os.environ:
        env[key] = os.environ[key]
plist = {
    'Label': label,
    'ProgramArguments': ['/bin/bash', str(base / 'run.sh')],
    'WorkingDirectory': str(base),
    'EnvironmentVariables': env,
    'RunAtLoad': True,
    'KeepAlive': True,
    'ThrottleInterval': 20,
    'StandardOutPath': str(runtime / 'service.log'),
    'StandardErrorPath': str(runtime / 'service.log'),
}
with (runtime / f'{label}.plist').open('wb') as handle:
    plistlib.dump(plist, handle)
PY
    if [[ "$1" == prepare ]]; then
      printf '%s\n' "$runtime/$label.plist"
      exit 0
    fi
    mkdir -p "$(dirname -- "$agent")"
    install -m 600 "$runtime/$label.plist" "$agent"
    launchctl bootstrap "$domain" "$agent"
    printf 'Loaded · http://127.0.0.1:%s/d/codex-health\nLog · %s/service.log\n' "$port" "$runtime"
    ;;
  stop)
    if launchctl print "$domain/$label" >/dev/null 2>&1; then
      launchctl bootout "$domain/$label"
    fi
    rm -f "$agent"
    ;;
  status)
    launchctl print "$domain/$label" | awk '/state =|pid =|last exit code =|stdout path =/'
    ;;
  *)
    echo "Usage: bash service.sh {start|status|stop}" >&2
    exit 2
    ;;
esac
