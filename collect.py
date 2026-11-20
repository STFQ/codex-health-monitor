#!/usr/bin/env python3
"""Collect local Codex and Clash diagnostic measurements and observed node labels."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

STAMP = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)')
FIELD = re.compile(r'\b([A-Za-z][A-Za-z0-9]*)=([^\s]+)')
PROXY_HOST = re.compile(r'-->\s*(?:[a-z0-9-]+\.)*(?:chatgpt\.com|openai\.com):\d+\b', re.I)
CODEX_ROUTE = re.compile(r' using codex\[(.{1,180})\]"\s*$')
FLOW_TARGET = re.compile(r'-->\s+(\[[a-f0-9:]+\]|[a-z0-9_.-]+):(\d+)\b', re.I)
DNS_UPSTREAM_URI = re.compile(
    r'(https?|h3|tls|quic|tcp|udp)://(\[[a-f0-9:]+\]|[a-z0-9_.-]+)(?::(\d+))?', re.I,
)
DNS_PLAIN_IP = re.compile(r'(?<![\w./])((?:\d{1,3}\.){3}\d{1,3})(?=$|[\s\'",#\]])')
DNS_PORTS = {'http': 80, 'https': 443, 'h3': 443, 'tls': 853, 'quic': 853, 'tcp': 53, 'udp': 53}
HOUR_MS = 3600000
DNS_LOOKUP_ERROR = re.compile(
    r'\bdns (?:resolve|lookup|request|query) failed\b|couldn.t find ip|no such host|name resolution', re.I,
)
DNS_CANCELED = re.compile(r'\b(?:operation was|context|request) cancel(?:l)?ed\b', re.I)
FLOW_ROUTE = re.compile(r' using (.{1,120}?)(?:\[(.{1,180})\])?"\s*$')
DIAL_GROUP = re.compile(r'\bdial (.{1,120}?) \(')
SENSITIVE_NODE_LABEL = re.compile(
    r'[a-z][a-z0-9+.-]*://|\b(?:\d{1,3}\.){3}\d{1,3}\b|\[[0-9a-f:]*:[0-9a-f:]*\]'
    r'|\b(?:token|secret|password|uuid)\s*[:=]|\b[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\b',
    re.I,
)
CALLS = {'function_call', 'custom_tool_call', 'web_search_call'}
TOOL_ITEMS = {'CommandExecution', 'McpToolCall', 'DynamicToolCall', 'WebSearch', 'Extension', 'FileChange'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS rpc(t INTEGER, ms INTEGER, queue_ms INTEGER, error INTEGER, timeout INTEGER);
CREATE TABLE IF NOT EXISTS connection(t INTEGER, event TEXT, ms INTEGER);
CREATE TABLE IF NOT EXISTS turns(t INTEGER, status TEXT, ttft_ms INTEGER, tool_ms INTEGER, total_ms INTEGER, tools INTEGER);
CREATE TABLE IF NOT EXISTS upstream(t INTEGER, event TEXT, cause TEXT);
CREATE TABLE IF NOT EXISTS proxy(t INTEGER, route TEXT, cause TEXT);
CREATE TABLE IF NOT EXISTS clash_timeline(t INTEGER, event TEXT, node TEXT, previous_t INTEGER, previous_node TEXT, cause TEXT);
CREATE TABLE IF NOT EXISTS dns_events(t INTEGER, event TEXT, target TEXT, route TEXT, node TEXT, cause TEXT);
CREATE TABLE IF NOT EXISTS dns_event_hours(hour INTEGER PRIMARY KEY, digest BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS sources(source TEXT, status TEXT, files INTEGER, last_write INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS capture(captured_at INTEGER, window_start INTEGER);
CREATE INDEX IF NOT EXISTS idx_rpc_time ON rpc(t);
CREATE INDEX IF NOT EXISTS idx_connection_time ON connection(t);
CREATE INDEX IF NOT EXISTS idx_turns_time ON turns(t);
CREATE INDEX IF NOT EXISTS idx_upstream_time ON upstream(t);
CREATE INDEX IF NOT EXISTS idx_proxy_time ON proxy(t);
CREATE INDEX IF NOT EXISTS idx_clash_timeline_time ON clash_timeline(t);
CREATE INDEX IF NOT EXISTS idx_dns_events_time ON dns_events(t);
"""

def epoch_ms(stamp):
    return round(datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp() * 1000)


def nonnegative(value):
    if isinstance(value, (float, int)):
        return round(value) if value >= 0 else None
    return int(value) if isinstance(value, str) and value.isdecimal() else None


def failure_cause(message):
    text = message.lower()
    if re.search(r'timed out|\btimeout\b|deadline exceeded|etimedout', text):
        return 'timeout'
    if re.search(r'connection refused|econnrefused', text):
        return 'refused'
    if re.search(r'connection reset|econnreset|broken pipe|without closing handshake', text):
        return 'reset'
    if re.search(r'\btls\b|handshake|certificate', text):
        return 'tls_handshake'
    if re.search(r'at capacity|overloaded|too many requests|\b429\b', text):
        return 'capacity'
    if re.search(r'\bdns\b|name resolution|no such host', text):
        return 'dns'
    return 'other'


def read_desktop(path):
    rpc, connection = [], []
    with path.open(errors='replace') as handle:
        for line in handle:
            if '[AppServerConnection]' not in line:
                continue
            match = STAMP.search(line[:70])
            if not match:
                continue
            at = epoch_ms(match[0])
            message = line.split('[AppServerConnection]', 1)[1].lstrip()
            fields = dict(FIELD.findall(message))
            if message.startswith('response_routed '):
                if fields.get('hadPending') != 'true' or 'errorCode' not in fields:
                    continue
                duration = nonnegative(fields.get('durationMs'))
                error = int(fields['errorCode'] != 'null')
                timeout = int(error and failure_cause(message) == 'timeout')
                rpc.append((at, duration, nonnegative(fields.get('queueWaitMs')), error, timeout))
            elif message.startswith('initialize_handshake_result '):
                event = 'init_ok' if fields.get('outcome') == 'success' else 'init_error'
                connection.append((at, event, nonnegative(fields.get('durationMs'))))
            elif message.startswith('app_server_connection.state_changed '):
                state = fields.get('next')
                cause = fields.get('cause')
                if state == 'disconnected':
                    event = 'stop' if cause == 'stop_process' else 'disconnect'
                elif state == 'connecting' and (nonnegative(fields.get('reconnectAttempt')) or 0) > 0:
                    event = 'reconnect'
                else:
                    continue
                connection.append((at, event, None))
            elif failure_cause(message) == 'timeout':
                connection.append((at, 'timeout', None))
    return {'rpc': rpc, 'connection': connection}


def read_rollout(path):
    rows = {}
    with path.open(errors='replace') as handle:
        active = None
        for line in handle:
            try:
                data = json.loads(line)
            except ValueError:
                continue  # The runtime may be appending an incomplete line.
            payload = data.get('payload') or {}
            event = data.get('type')
            kind = payload.get('type')
            if event == 'session_meta':
                if payload.get('originator') != 'Codex Desktop' or payload.get('source') != 'vscode':
                    return {}
            elif event == 'event_msg' and kind == 'task_started' and payload.get('turn_id'):
                key = payload['turn_id']
                active = rows.setdefault(key, [epoch_ms(data['timestamp']), 'unknown', None, None, None, 0, None])
            elif active is not None and event == 'response_item' and kind in CALLS:
                active[5] += 1
                if active[6] is None:
                    active[6] = max(0, epoch_ms(data['timestamp']) - active[0])
            elif active is not None and event == 'event_msg' and kind == 'item_completed':
                if (payload.get('item') or {}).get('type') in TOOL_ITEMS:
                    started = nonnegative(payload.get('started_at_ms'))
                    if started is not None and started >= active[0] - 250:
                        elapsed = max(0, started - active[0])
                        active[3] = elapsed if active[3] is None else min(elapsed, active[3])
            elif active is not None and event == 'event_msg' and kind in ('task_complete', 'turn_aborted'):
                if rows.get(payload.get('turn_id')) is active:
                    active[1] = 'recorded_end' if kind == 'task_complete' else 'interrupted'
                    active[2] = nonnegative(payload.get('time_to_first_token_ms'))
                    active[4] = nonnegative(payload.get('duration_ms'))
                    if active[4] is None:
                        active[4] = max(0, epoch_ms(data['timestamp']) - active[0])
                    active = None
    for row in rows.values():
        fallback = row.pop()
        if row[3] is None:
            row[3] = fallback
    return rows


def host_key(host):
    return host.lower().strip('[]').rstrip('.')


def read_dns_endpoints(path):
    """Discover only resolver addresses from the profile's DNS block."""
    endpoints = set()
    in_dns = False
    resolver_keys = {
        'nameserver', 'fallback', 'default-nameserver', 'direct-nameserver',
        'proxy-server-nameserver', 'nameserver-policy', 'proxy-server-nameserver-policy',
    }
    field = None
    with path.open(errors='replace') as handle:
        for line in handle:
            if not in_dns:
                in_dns = bool(re.match(r'^dns:\s*(?:#.*)?$', line))
                continue
            if re.match(r'^[a-z][a-z0-9_-]*\s*:', line, re.I):
                break
            if line.lstrip().startswith('#'):
                continue
            key = re.match(r'^ {2}([a-z-]+)\s*:', line)
            if key:
                field = key[1]
            if field not in resolver_keys:
                continue
            for scheme, host, port in DNS_UPSTREAM_URI.findall(line):
                endpoints.add((host_key(host), int(port) if port else DNS_PORTS[scheme.lower()]))
            without_urls = DNS_UPSTREAM_URI.sub('', line)
            for host in DNS_PLAIN_IP.findall(without_urls):
                endpoints.add((host, 53))
    return endpoints


def safe_label(label):
    cleaned = label.strip() if label else ''
    return cleaned if cleaned and not SENSITIVE_NODE_LABEL.search(cleaned) else None


def flow_route(line):
    routed = FLOW_ROUTE.search(line)
    if routed:
        return safe_label(routed[1]), safe_label(routed[2])
    attempted = DIAL_GROUP.search(line)
    return safe_label(attempted[1]) if attempted else None, None


def dns_target_scope(host):
    for suffix in ('chatgpt.com', 'openai.com', 'push.apple.com', 'apple.com', 'byted.org'):
        if host == suffix or host.endswith('.' + suffix):
            return suffix
    return '其他域名' if host else '域名未记录'


def read_proxy(path):
    errors, observations, codex_timeouts, dns_rows = [], [], [], []
    with path.open(errors='replace') as handle:
        for line in handle:
            is_error = 'error:' in line
            resolver_flow = 'mihomo -->' in line
            tracked_proxy = (is_error or ' using codex[' in line) and PROXY_HOST.search(line)
            lookup_error = is_error and DNS_LOOKUP_ERROR.search(line)
            if not (tracked_proxy or resolver_flow or lookup_error):
                continue
            match = STAMP.search(line[:80])
            if not match:
                continue
            at = epoch_ms(match[0])
            if resolver_flow or lookup_error:
                socket_target = FLOW_TARGET.search(line)
                host = host_key(socket_target[1]) if socket_target else ''
                route, node = flow_route(line)
                if lookup_error and not resolver_flow:
                    dns_rows.append((at, 'resolve_error', dns_target_scope(host), route, node, 'dns', None))
                if resolver_flow and socket_target:
                    port = int(socket_target[2])
                    endpoint = (host, port)
                    target = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
                    if is_error and DNS_CANCELED.search(line):
                        dns_rows.append((at, 'upstream_canceled', target, route, node, 'canceled', endpoint))
                    elif is_error:
                        cause = failure_cause(line.rsplit(' error:', 1)[-1])
                        dns_rows.append((at, 'upstream_error', target, route, node, cause, endpoint))
                    elif route:
                        dns_rows.append((at, 'upstream_routed', target, route, node, None, endpoint))
            if not tracked_proxy:
                continue
            if is_error:
                route = 'direct' if re.search(r'\bdial DIRECT\b', line) else 'proxy'
                error_text = line.rsplit(' error:', 1)[-1]
                cause = failure_cause(error_text)
                errors.append((at, route, cause))
                if cause == 'timeout' and 'dial codex (' in line:
                    stage = 'connect_timeout' if ' connect error:' in line else 'dial_timeout'
                    codex_timeouts.append((at, stage))
            else:
                node = CODEX_ROUTE.search(line)
                if node:
                    label = safe_label(node[1])
                    if label:
                        observations.append((at, label))
    return errors, observations, codex_timeouts, dns_rows


def clash_timeline(observations, timeouts):
    """Correlate exact timeout log times with node labels from earlier routed connections."""
    merged = [(at, False, stage) for at, stage in timeouts]
    merged.extend((at, True, node) for at, node in observations)
    merged.sort(key=lambda event: (event[0], event[1]))
    last_at, last_node = None, None
    rows = []
    for at, observed, value in merged:
        if not observed:
            rows.append((at, 'timeout', None, last_at, last_node, value))
            continue
        if last_node is not None and value != last_node:
            rows.append((at, 'node_observed', value, last_at, last_node, None))
        last_at, last_node = at, value
    return rows


def sync_dns_events(con, rows):
    """Replace only the hourly buckets whose log records have changed."""
    hours = {}
    for row in rows:
        hours.setdefault(row[0] // HOUR_MS, []).append(row)
    previous = dict(con.execute('SELECT hour, digest FROM dns_event_hours'))
    for hour in previous.keys() | hours.keys():
        batch = hours.get(hour)
        if batch is None:
            digest = None
        else:
            digest = hashlib.sha256(json.dumps(batch, ensure_ascii=False).encode()).digest()
        if digest == previous.get(hour):
            continue
        con.execute('DELETE FROM dns_events WHERE t >= ? AND t < ?', (hour * HOUR_MS, (hour + 1) * HOUR_MS))
        if batch is None:
            con.execute('DELETE FROM dns_event_hours WHERE hour = ?', (hour,))
        else:
            con.executemany('INSERT INTO dns_events VALUES (?, ?, ?, ?, ?, ?)', batch)
            con.execute('INSERT OR REPLACE INTO dns_event_hours VALUES (?, ?)', (hour, digest))


def read_upstream(path, lower, upper):
    events = []
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as source:
        for seconds, nanos, target, body in source.execute("""
            SELECT ts, ts_nanos, target, feedback_log_body FROM logs
            WHERE ts BETWEEN ? AND ? AND feedback_log_body IS NOT NULL AND target IN (
              'codex_api::endpoint::responses_websocket', 'codex_core::responses_retry',
              'codex_core::session::startup_prewarm', 'codex_core::client'
            )
        """, (lower // 1000, upper // 1000)):
            text = body.lower()
            event = None
            if target.endswith('responses_websocket') and 'failed to connect to responses websocket' in text:
                event = 'ws_connect_error'
            elif target.endswith('responses_retry') and 'retrying sampling request' in text:
                event = 'model_retry'
            elif target.endswith('startup_prewarm') and 'stream disconnected' in text:
                event = 'prewarm_error'
            elif target.endswith('::client') and re.search(r'fall(?:ing)? back to http', text):
                event = 'http_fallback'
            if event:
                events.append((seconds * 1000 + (nanos or 0) // 1000000, event, failure_cause(text)))
    return events


class FileCache:
    def __init__(self):
        self.entries = {}

    def read(self, paths, parser):
        parsed = []
        newest = None
        errors = 0
        for path in paths:
            try:
                stat = path.stat()
                current = (stat.st_size, stat.st_mtime_ns)
                previous = self.entries.get(path)
                if previous is None or previous[0] != current:
                    self.entries[path] = current, parser(path)
                parsed.append(self.entries[path][1])
                newest = max(newest or 0, stat.st_mtime_ns // 1000000)
            except (OSError, ValueError):
                errors += 1
        return parsed, newest, errors

    def keep(self, paths):
        self.entries = {path: data for path, data in self.entries.items() if path in paths}


def source_info(label, files, newest, errors, note):
    status = '部分读取失败' if errors and files else '读取失败' if errors else '已读取' if files else '未找到'
    return label, status, files, newest, note


def collect(args, cache):
    now = int(time.time() * 1000)
    cutoff = now - args.days * 86400000
    today = datetime.now().date()
    dates = [today - timedelta(days=offset) for offset in range(-1, args.days + 2)]
    desktop_files = {
        path for day in dates for path in (args.desktop / day.strftime('%Y/%m/%d')).glob('codex-desktop-*.log')
    }
    recent_after = cutoff / 1000
    turn_files = set()
    for path in (args.codex_home / 'sessions').glob('*/*/*/rollout-*.jsonl'):
        try:
            if path.stat().st_mtime >= recent_after:
                turn_files.add(path)
        except FileNotFoundError:
            continue
    proxy_files = {args.clash / f'core-{day.isoformat()}.log' for day in dates}
    proxy_files = {path for path in proxy_files if path.is_file()}
    clash_home = args.clash.parent
    dns_profiles = {clash_home / 'work/config.yaml', clash_home / 'mihomo.yaml'}
    dns_profiles.update((clash_home / 'profiles').glob('*.yaml'))
    dns_profiles.update((clash_home / 'profiles').glob('*.yml'))
    dns_profiles = {path for path in dns_profiles if path.is_file()}
    cache.keep(desktop_files | turn_files | proxy_files | dns_profiles)
    desktop, desktop_time, desktop_errors = cache.read(desktop_files, read_desktop)
    raw_turns, turn_time, turn_errors = cache.read(turn_files, read_rollout)
    proxy, proxy_time, proxy_errors = cache.read(proxy_files, read_proxy)
    dns_endpoints, _, _ = cache.read(dns_profiles, read_dns_endpoints)
    dns_endpoints = set().union(*dns_endpoints)

    tables = {name: [] for name in ('rpc', 'connection', 'turns', 'upstream', 'proxy', 'clash_timeline')}
    for file in desktop:
        for name, rows in file.items():
            tables[name].extend(rows)
    merged = {}
    for file in raw_turns:
        for key, row in file.items():
            if key not in merged or (row[1] != 'unknown' and merged[key][1] == 'unknown'):
                merged[key] = row
    tables['turns'] = list(merged.values())
    tables['proxy'] = [row for errors, _, _, _ in proxy for row in errors]
    tables['clash_timeline'] = clash_timeline(
        (row for _, observations, _, _ in proxy for row in observations),
        (row for _, _, timeouts, _ in proxy for row in timeouts),
    )
    dns_events = [
        row[:-1] for _, _, _, dns_rows in proxy for row in dns_rows
        if cutoff <= row[0] <= now and (row[-1] is None or row[-1] in dns_endpoints)
    ]

    log_db = args.codex_home / 'logs_2.sqlite'
    log_time, log_files, log_errors = None, 0, 0
    if log_db.is_file():
        try:
            log_time = log_db.stat().st_mtime_ns // 1000000
            wal = log_db.with_name(log_db.name + '-wal')
            try:
                log_time = max(log_time, wal.stat().st_mtime_ns // 1000000)
            except FileNotFoundError:
                pass
            tables['upstream'] = read_upstream(log_db, cutoff, now)
            log_files = 1
        except (sqlite3.Error, OSError):
            log_errors = 1
    sources = [
        source_info('桌面端', len(desktop), desktop_time, desktop_errors, 'JSON-RPC 返回、初始化和连接状态'),
        source_info('会话记录', len(raw_turns), turn_time, turn_errors, '匹配收到开始事件的桌面端会话'),
        source_info('Codex 运行时', log_files, log_time, log_errors, '本地保留的上游异常与回退记录'),
        source_info('Clash / Mihomo', len(proxy), proxy_time, proxy_errors, '拨号错误、codex 节点与 DNS 日志'),
    ]
    args.database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.database, timeout=15) as con:
        con.executescript(SCHEMA)
        con.execute('BEGIN IMMEDIATE')
        for name, rows in tables.items():
            con.execute('DELETE FROM ' + name)
            selected = [row for row in rows if cutoff <= row[0] <= now]
            if selected:
                placeholders = ','.join('?' for _ in selected[0])
                con.executemany(f'INSERT INTO {name} VALUES ({placeholders})', selected)
            print(f'{name}={len(selected)}', end=' ', flush=True)
        sync_dns_events(con, dns_events)
        print(f'dns_events={len(dns_events)}', end=' ', flush=True)
        con.execute('DELETE FROM sources')
        con.executemany('INSERT INTO sources VALUES (?, ?, ?, ?, ?)', sources)
        con.execute('DELETE FROM capture')
        con.execute('INSERT INTO capture VALUES (?, ?)', (now, cutoff))
    print(flush=True)


def main():
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--days', type=int, choices=range(1, 32), default=7, metavar='1–31')
    parser.add_argument('--follow', type=int, default=0, metavar='SECONDS')
    parser.add_argument('--codex-home', type=Path, default=Path(os.getenv('CODEX_HOME', home / '.codex')))
    parser.add_argument('--desktop', type=Path, default=home / 'Library/Logs/com.openai.codex')
    parser.add_argument('--clash', type=Path, default=home / 'Library/Application Support/mihomo-party/logs')
    args = parser.parse_args()
    if args.follow and args.follow < 15:
        parser.error('--follow must be at least 15 seconds')
    args.codex_home = args.codex_home.expanduser().resolve()
    cache = FileCache()
    while True:
        collect(args, cache)
        if not args.follow:
            return
        time.sleep(args.follow)


if __name__ == '__main__':
    main()
