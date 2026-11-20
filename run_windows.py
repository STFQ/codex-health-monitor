"""Run Codex Health with standalone Grafana on Windows."""

import argparse
import errno
import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.request import ProxyHandler, build_opener, urlopen

from collect import FileCache, add_source_arguments, collect

BASE = Path(__file__).resolve().parent
GRAFANA_VERSION = '13.2.3'
GRAFANA_BUILD = '36482603486'
GRAFANA_SHA256 = 'dafe883a7c15543c6d6710b868eb8b4053353441ce7ceacf150942b8de3960f0'
PLUGIN = 'frser-sqlite-datasource'
PLUGIN_VERSION = '4.0.6'
INTERVAL = 15


@contextmanager
def service_lock(runtime):
    import msvcrt

    with (runtime / 'service.lock').open('a+b') as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            yield False
        else:
            try:
                yield True
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def active_service(runtime):
    if not runtime.is_dir():
        return None
    with service_lock(runtime) as acquired:
        if acquired:
            return None
    try:
        return json.loads((runtime / 'service.json').read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def download(url, path, check_stop, checksum=None):
    def verified(file):
        if not file.is_file():
            return False
        if checksum is None:
            return True
        with file.open('rb') as handle:
            return hashlib.file_digest(handle, 'sha256').hexdigest() == checksum

    if verified(path):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + '.part')
    print(f'Downloading {path.name}', flush=True)
    try:
        with urlopen(url, timeout=30) as source, partial.open('wb') as output:
            while chunk := source.read(1024 * 1024):
                check_stop()
                output.write(chunk)
        if not verified(partial):
            raise OSError(f'SHA-256 mismatch for {path.name}')
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def grafana_command(home):
    executable = home / 'bin/grafana.exe'
    if executable.is_file():
        return [str(executable), 'server', '--homepath', str(home)]
    executable = home / 'bin/grafana-server.exe'
    if executable.is_file():
        return [str(executable), '--homepath', str(home)]
    raise OSError(f'Grafana executable missing in {home / "bin"}')


def install(runtime, check_stop):
    downloads = runtime / 'downloads'
    if 'GRAFANA_HOME' in os.environ:
        home = Path(os.environ['GRAFANA_HOME']).expanduser().resolve()
    else:
        home = runtime / f'grafana-{GRAFANA_VERSION}-windows-amd64'
        if not home.is_dir():
            asset = f'grafana_{GRAFANA_VERSION}_{GRAFANA_BUILD}_windows_amd64.tar.gz'
            archive = downloads / f'{home.name}.tar.gz'
            url = f'https://dl.grafana.com/grafana/release/{GRAFANA_VERSION}/{asset}'
            download(url, archive, check_stop, GRAFANA_SHA256)
            print('Extracting Grafana', flush=True)
            with tempfile.TemporaryDirectory(dir=runtime) as temporary:
                stage = Path(temporary)
                with tarfile.open(archive, 'r:gz') as package:
                    package.extractall(stage, filter='data')
                extracted = next(stage.glob('*/conf/defaults.ini'), None)
                if extracted is None:
                    raise OSError(f'Grafana layout missing in {archive.name}')
                extracted_home = extracted.parent.parent
                grafana_command(extracted_home)
                extracted_home.replace(home)
    command = grafana_command(home)

    plugins = runtime / 'plugins'
    plugins.mkdir(exist_ok=True)
    plugin = plugins / PLUGIN
    plugin_binary = 'gpx_sqlite-datasource_windows_amd64.exe'
    if not plugin.is_dir():
        archive = downloads / f'sqlite-plugin-{PLUGIN_VERSION}-windows-amd64.zip'
        url = f'https://grafana.com/api/plugins/{PLUGIN}/versions/{PLUGIN_VERSION}/download?os=windows&arch=amd64'
        download(url, archive, check_stop)
        with tempfile.TemporaryDirectory(dir=runtime) as temporary:
            with zipfile.ZipFile(archive) as package:
                package.extractall(temporary)
            staged = Path(temporary) / PLUGIN
            if not (staged / plugin_binary).is_file():
                raise OSError('SQLite plugin package has no Windows x64 backend')
            staged.replace(plugin)
    if not (plugin / plugin_binary).is_file():
        raise OSError(f'SQLite plugin Windows backend missing in {plugin}')
    return command, home


def grafana_env(runtime, database, port):
    provisioning = runtime / 'provisioning'
    dashboards = runtime / 'dashboards'
    for path in (dashboards, runtime / 'data', runtime / 'logs',
                 *(provisioning / subdir for subdir in ('datasources', 'dashboards', 'plugins', 'alerting'))):
        path.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BASE / 'dashboard.json', dashboards / 'codex-health.json')
    configs = {
        'datasources/main.yaml': {
            'apiVersion': 1,
            'datasources': [{
                'name': 'Codex Health SQLite', 'type': PLUGIN, 'uid': 'codex-health-local',
                'access': 'proxy', 'editable': False,
                'jsonData': {'path': database.as_posix(), 'pathOptions': 'mode=ro', 'attachLimit': 0},
            }],
        },
        'dashboards/main.yaml': {
            'apiVersion': 1,
            'providers': [{
                'name': 'Codex Health', 'type': 'file', 'options': {'path': str(dashboards)},
            }],
        },
    }
    for name, config in configs.items():
        (provisioning / name).write_text(json.dumps(config) + '\n', encoding='utf-8')
    return {
        **os.environ,
        'GF_PATHS_DATA': str(runtime / 'data'),
        'GF_PATHS_LOGS': str(runtime / 'logs'),
        'GF_PATHS_PLUGINS': str(runtime / 'plugins'),
        'GF_PATHS_PROVISIONING': str(provisioning),
        'GF_SERVER_HTTP_ADDR': '127.0.0.1',
        'GF_SERVER_HTTP_PORT': str(port),
        'GF_AUTH_ANONYMOUS_ENABLED': 'true',
        'GF_AUTH_ANONYMOUS_ORG_ROLE': 'Viewer',
        'GF_SECURITY_ADMIN_PASSWORD': secrets.token_hex(24),
        'GF_ANALYTICS_REPORTING_ENABLED': 'false',
        'GF_ANALYTICS_CHECK_FOR_UPDATES': 'false',
        'GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES': 'false',
        'GF_PLUGINS_PLUGIN_ADMIN_ENABLED': 'false',
        'GF_PLUGINS_PREINSTALL_DISABLED': 'true',
        'GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH': str(dashboards / 'codex-health.json'),
    }


def status(runtime):
    state = active_service(runtime)
    if state is None:
        print('Stopped')
        return
    if state:
        url = f'http://127.0.0.1:{state["port"]}'
        try:
            with build_opener(ProxyHandler({})).open(url + '/api/health', timeout=2) as response:
                if json.load(response).get('database') == 'ok':
                    print(f'Running: {url}/d/codex-health')
                    print(f'Log: {runtime / "service.log"}')
                    return
        except (OSError, ValueError):
            pass
    print('Starting')
    print(f'Log: {runtime / "service.log"}')


def stop(runtime):
    deadline = time.monotonic() + 60
    instance = None
    while time.monotonic() < deadline:
        state = active_service(runtime)
        if state is None or (state and instance and state['instance'] != instance):
            print('Stopped')
            return
        if state and instance is None:
            instance = state['instance']
            (runtime / f'stop-{instance}').touch()
        time.sleep(0.25)
    raise OSError(f'Still stopping. Check {runtime / "service.log"}')


def start(runtime, args):
    if active_service(runtime) is None:
        command = [sys.executable, '-X', 'utf8', '-u', str(BASE / 'run_windows.py'), 'run',
                   '--port', str(args.port), '--days', str(args.days)]
        for option, value in (('--codex-home', args.codex_home), ('--clash', args.clash),
                              ('--desktop', args.desktop)):
            if value is not None:
                command.extend([option, str(value)])
        with (runtime / 'service.log').open('ab') as log:
            worker = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        deadline = time.monotonic() + 10
        while active_service(runtime) is None:
            if worker.poll() is not None or time.monotonic() >= deadline:
                raise OSError(f'Windows monitor could not start. Check {runtime / "service.log"}')
            time.sleep(0.1)
    status(runtime)


def run(runtime, args):
    with service_lock(runtime) as acquired:
        if not acquired:
            print('The monitor is already active.')
            return
        instance = secrets.token_hex(12)
        state = runtime / 'service.json'
        request = runtime / f'stop-{instance}'
        state.write_text(json.dumps({'port': args.port, 'instance': instance}) + '\n', encoding='utf-8')
        grafana = None

        def check_stop():
            if request.exists():
                raise KeyboardInterrupt

        try:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(('127.0.0.1', args.port))
            command, home = install(runtime, check_stop)
            check_stop()
            args.database = runtime / 'codex-health.sqlite'
            cache = FileCache()
            collect(args, cache)
            check_stop()
            grafana = subprocess.Popen(command, cwd=home, env=grafana_env(runtime, args.database, args.port))
            print(f'Grafana: http://127.0.0.1:{args.port}/d/codex-health', flush=True)
            next_capture = time.monotonic() + INTERVAL
            while grafana.poll() is None:
                check_stop()
                if time.monotonic() >= next_capture:
                    collect(args, cache)
                    next_capture = time.monotonic() + INTERVAL
                time.sleep(0.25)
            raise OSError(f'Grafana exited with status {grafana.returncode}')
        except KeyboardInterrupt:
            print('Stopping the monitor.', flush=True)
        finally:
            try:
                if grafana is not None and grafana.poll() is None:
                    # Grafana starts the SQLite backend as a separate process.
                    taskkill = Path(os.environ['SystemRoot']) / 'System32/taskkill.exe'
                    subprocess.run([str(taskkill), '/PID', str(grafana.pid), '/T', '/F'], check=True)
                    grafana.wait()
            finally:
                state.unlink(missing_ok=True)
                request.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('run', 'start', 'status', 'stop'))
    parser.add_argument('--port', type=int, default=os.getenv('GRAFANA_PORT', '3308'))
    parser.add_argument('--days', type=int, choices=range(1, 32), default=os.getenv('CODEX_HEALTH_DAYS', '7'),
                        metavar='1–31')
    add_source_arguments(parser)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('This launcher requires Windows')
    if not hasattr(tarfile, 'data_filter'):
        parser.error('This launcher requires Python 3.11.4 or later')
    if platform.machine().lower() not in ('amd64', 'x86_64'):
        parser.error('The SQLite plugin requires Windows x64')
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    if not 1 <= args.days <= 31:
        parser.error('--days must be between 1 and 31')
    runtime = Path(os.getenv('GRAFANA_RUNTIME_DIR', BASE / '.grafana-runtime')).expanduser().resolve()
    try:
        if args.command in ('start', 'run'):
            runtime.mkdir(parents=True, exist_ok=True)
            if args.command == 'start':
                start(runtime, args)
            else:
                run(runtime, args)
        elif args.command == 'status':
            status(runtime)
        else:
            stop(runtime)
    except (OSError, subprocess.CalledProcessError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f'Codex Health: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
