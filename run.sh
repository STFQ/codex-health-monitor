#!/bin/bash
set -euo pipefail
umask 077

base="$(cd -- "$(dirname -- "$0")" && pwd)"
runtime="${GRAFANA_RUNTIME_DIR:-$base/.grafana-runtime}"
mkdir -p "$runtime"
runtime="$(cd -- "$runtime" && pwd)"
version=13.2.3
plugin=frser-sqlite-datasource
plugin_version=4.0.6
port="${GRAFANA_PORT:-3308}"
days="${CODEX_HEALTH_DAYS:-7}"
database="$base/codex-health.sqlite"

case "$(uname -m)" in
  arm64) arch=arm64; hash=248a51bcfacdb1ec642006cee46b4de44a34bcf41ddea88d96a4605e2e9d808c ;;
  x86_64) arch=amd64; hash=59d08d3ef611a49239bf3b54cd1cb1b215611a3f6cd37bb6e6abe40dda181319 ;;
  *) echo "Unsupported Mac architecture" >&2; exit 1 ;;
esac

if [[ -n "${GRAFANA_HOME:-}" ]]; then
  grafana="$GRAFANA_HOME"
else
  grafana="$runtime/grafana-v$version"
  if [[ ! -x "$grafana/bin/grafana" ]]; then
    archive="$runtime/grafana.tar.gz"
    if [[ ! -f "$archive" ]]; then
      curl --fail --location --retry 2 --output "$archive.part" \
        "https://dl.grafana.com/grafana/release/$version/grafana_${version}_36482603486_darwin_${arch}.tar.gz"
      mv "$archive.part" "$archive"
    fi
    printf '%s  %s\n' "$hash" "$archive" | shasum -a 256 -c
    mkdir -p "$grafana"
    tar -xzf "$archive" -C "$grafana" --strip-components 1
  fi
fi

plugins="$runtime/plugins"
if [[ ! -f "$plugins/$plugin/plugin.json" ]]; then
  mkdir -p "$plugins"
  if [[ ! -f "$runtime/sqlite-plugin.zip" ]]; then
    curl --fail --location --retry 2 --output "$runtime/sqlite-plugin.zip.part" \
      "https://grafana.com/api/plugins/$plugin/versions/$plugin_version/download?os=darwin&arch=$arch"
    mv "$runtime/sqlite-plugin.zip.part" "$runtime/sqlite-plugin.zip"
  fi
  unzip -oq "$runtime/sqlite-plugin.zip" -d "$plugins"
fi

mkdir -p "$runtime/provisioning/datasources" "$runtime/provisioning/dashboards" \
  "$runtime/provisioning/plugins" "$runtime/provisioning/alerting" \
  "$runtime/dashboards" "$runtime/data" "$runtime/logs"
cp "$base/dashboard.json" "$runtime/dashboards/codex-health.json"
python3 - "$runtime" "$database" <<'PY'
import json
import sys
from pathlib import Path

runtime, database = map(Path, sys.argv[1:3])
requirements = {
    'datasources/main.yaml': {
        'apiVersion': 1,
        'datasources': [{
            'name': 'Codex Health SQLite', 'type': 'frser-sqlite-datasource',
            'uid': 'codex-health-local', 'access': 'proxy', 'editable': False,
            'jsonData': {'path': str(database), 'pathOptions': 'mode=ro', 'attachLimit': 0},
        }],
    },
    'dashboards/main.yaml': {
        'apiVersion': 1,
        'providers': [{
            'name': 'Codex Health', 'type': 'file',
            'options': {'path': str(runtime / 'dashboards')},
        }],
    },
}
for name, content in requirements.items():
    (runtime / 'provisioning' / name).write_text(json.dumps(content, ensure_ascii=False) + '\n')
PY

if [[ ! -f "$database" ]]; then
  python3 "$base/collect.py" --database "$database" --days "$days"
fi
collector_pid=''
grafana_pid=''
cleanup() {
  if [[ -n "$grafana_pid" ]]; then
    kill "$grafana_pid" 2>/dev/null || true
    wait "$grafana_pid" 2>/dev/null || true
  fi
  if [[ -n "$collector_pid" ]]; then
    kill "$collector_pid" 2>/dev/null || true
    wait "$collector_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ "${CODEX_HEALTH_SNAPSHOT_ONLY:-0}" != 1 ]]; then
  python3 "$base/collect.py" --database "$database" --days "$days" --follow 15 &
  collector_pid=$!
fi

export GF_PATHS_DATA="$runtime/data"
export GF_PATHS_LOGS="$runtime/logs"
export GF_PATHS_PLUGINS="$plugins"
export GF_PATHS_PROVISIONING="$runtime/provisioning"
export GF_SERVER_HTTP_ADDR=127.0.0.1
export GF_SERVER_HTTP_PORT="$port"
export GF_AUTH_ANONYMOUS_ENABLED=true
export GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer
export GF_SECURITY_ADMIN_PASSWORD="$(openssl rand -hex 24)"
export GF_ANALYTICS_REPORTING_ENABLED=false
export GF_ANALYTICS_CHECK_FOR_UPDATES=false
export GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false
export GF_PLUGINS_PLUGIN_ADMIN_ENABLED=false
export GF_PLUGINS_PREINSTALL_DISABLED=true
export GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH="$runtime/dashboards/codex-health.json"

echo "Codex Health Grafana · http://127.0.0.1:$port/d/codex-health"
"$grafana/bin/grafana" server --homepath "$grafana" &
grafana_pid=$!
while kill -0 "$grafana_pid" 2>/dev/null; do
  if [[ -n "$collector_pid" ]] && ! kill -0 "$collector_pid" 2>/dev/null; then
    echo "Codex Health collector stopped" >&2
    exit 1
  fi
  sleep 2
done
wait "$grafana_pid"
