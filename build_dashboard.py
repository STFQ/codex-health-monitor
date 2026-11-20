import json
from pathlib import Path

PLUGIN = 'frser-sqlite-datasource'
DS = {'type': PLUGIN, 'uid': 'codex-health-local'}
WINDOW = 't >= $__from AND t < $__to'
INTERVAL = """CASE
    WHEN $__to - $__from <= 3600000 THEN 60000
    WHEN $__to - $__from <= 21600000 THEN 300000
    WHEN $__to - $__from <= 86400000 THEN 900000
    ELSE 3600000 END"""
BUCKET = f'(t / ({INTERVAL})) * ({INTERVAL})'
COLORS = ['#5794F2', '#73BF69', '#FF9830', '#F2495C', '#B877D9']
panels = []


def quantile(table, pairs, grouped=True):
    bucket = BUCKET if grouped else '0'
    selects = [
        f"SELECT {bucket} AS bucket, '{label}' AS metric, {column} AS value "
        f"FROM {table} WHERE {WINDOW} AND {column} IS NOT NULL"
        for column, label, _ in pairs
    ]
    values = '\nUNION ALL\n'.join(dict.fromkeys(selects))
    partition = """ROW_NUMBER() OVER (PARTITION BY bucket, metric ORDER BY value) AS pos,
        COUNT(*) OVER (PARTITION BY bucket, metric) AS total"""
    fields = [
        f"""MAX(CASE WHEN metric = '{label}' AND pos = (total * {pct} + 99) / 100
        THEN value END) AS "{label} P{pct}" """
        for _, label, pct in pairs
    ]
    return f"""WITH points AS ({values}),
    ranks AS (SELECT *, {partition} FROM points)
    SELECT {('bucket / 1000 AS time, ' if grouped else '')}{', '.join(fields)}
    FROM ranks
    {'GROUP BY bucket ORDER BY bucket' if grouped else ''}"""


def summed(table, fields):
    return f"""SELECT {BUCKET} / 1000 AS time, {', '.join(fields)}
      FROM {table} WHERE {WINDOW} GROUP BY time ORDER BY time"""


def count_if(predicate, label):
    return f'''SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END) AS "{label}"'''


def base(title, typ, x, y, w, h, desc=''):
    return {'id': len(panels) + 1, 'title': title, 'type': typ,
            'gridPos': {'x': x, 'y': y, 'w': w, 'h': h}, 'description': desc}


def add(title, typ, x, y, w, h, sql, desc='', time_columns=()):
    p = base(title, typ, x, y, w, h, desc)
    p['datasource'] = DS
    p['targets'] = [{'refId': 'A', 'datasource': DS, 'rawQueryText': sql, 'queryText': sql,
                     'queryType': 'table', 'timeColumns': list(time_columns)}]
    panels.append(p)
    return p


def stat(title, x, sql, desc, unit='short', color='#5794F2', *, y=0, w=4):
    p = add(title, 'stat', x, y, w, 4, sql, desc)
    fields = {'unit': unit, 'noValue': '暂无样本', 'color': {'mode': 'fixed', 'fixedColor': color}}
    if unit == 'suffix: 条':
        fields.update(decimals=0, color={'mode': 'thresholds'}, thresholds={
            'mode': 'absolute',
            'steps': [{'value': None, 'color': '#73BF69'}, {'value': 1, 'color': color}],
        })
    p['fieldConfig'] = {'defaults': fields, 'overrides': []}
    p['options'] = {'colorMode': 'value', 'graphMode': 'none', 'textMode': 'value',
                    'justifyMode': 'auto', 'reduceOptions': {'calcs': ['lastNotNull'], 'values': False}}


def row(title, y):
    p = base(title, 'row', 0, y, 24, 1)
    p.update(collapsed=False, panels=[])
    panels.append(p)


def graph(title, x, y, w, sql, names, desc, unit='short', bars=False):
    p = add(title, 'timeseries', x, y, w, 8, sql, desc, ['time'])
    p['fieldConfig'] = {
        'defaults': {'unit': unit, 'min': 0, 'color': {'mode': 'palette-classic'},
                     'custom': {'drawStyle': 'bars' if bars else 'line', 'fillOpacity': 32 if bars else 9,
                                'lineWidth': 2, 'showPoints': 'auto', 'pointSize': 4, 'spanNulls': False,
                                'axisBorderShow': False, 'stacking': {'mode': 'none', 'group': 'A'}}},
        'overrides': [{'matcher': {'id': 'byName', 'options': name},
                       'properties': [{'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': color}}]}
                      for name, color in zip(names, COLORS)],
    }
    p['options'] = {'legend': {'displayMode': 'list', 'placement': 'bottom', 'showLegend': True},
                    'tooltip': {'mode': 'multi', 'sort': 'desc'}}
    return p


def table(title, x, y, w, h, sql, desc, time_columns=(), ms=()):
    p = add(title, 'table', x, y, w, h, sql, desc, time_columns)
    p['fieldConfig'] = {
        'defaults': {'noValue': '—', 'custom': {'align': 'auto', 'cellOptions': {'type': 'auto'}}},
        'overrides': [{'matcher': {'id': 'byName', 'options': name},
                       'properties': [{'id': 'unit', 'value': 'ms'}]} for name in ms],
    }
    p['options'] = {'showHeader': True, 'cellHeight': 'sm', 'footer': {'show': False}}


stat('桌面请求轮次', 20, f'SELECT COUNT(*) AS value FROM turns WHERE {WINDOW}',
     '采样窗口内有开始事件的主会话轮次。', 'suffix: 轮')
stat('首 token · P95', 12, quantile('turns', [('ttft_ms', '首 token', 95)], False),
     '从开始到模型首 token。包括后台记录到的 token。', 'ms', '#73BF69')
stat('App Server RPC · P95', 0, quantile('rpc', [('ms', 'RPC', 95)], False),
     '桌面端已匹配到的 JSON-RPC 返回耗时，含各类管理和后台请求。', 'ms', y=58, w=14)
stat('RPC 返回错误率', 14, f'SELECT 100.0 * AVG(error) AS value FROM rpc WHERE {WINDOW}',
     '分母是具有返回日志的请求，包含业务错误。', 'percent', '#FF9830', y=58, w=10)
stat('模型连接失败', 4,
     f"SELECT COUNT(*) AS value FROM upstream WHERE {WINDOW} AND event='ws_connect_error'",
     '模型 WebSocket 连接失败的日志条数。重试可能产生多条。', 'suffix: 条', '#F2495C')
stat('Clash 拨号超时', 0,
     f"""SELECT CASE WHEN EXISTS (SELECT 1 FROM sources WHERE source='Clash / Mihomo' AND files > 0)
     THEN COUNT(*) END AS value FROM proxy WHERE {WINDOW} AND cause='timeout'""",
     '所选时间内访问 ChatGPT 或 OpenAI 域名的拨号超时日志条数。0 条表示可用日志里没有匹配记录。',
     'suffix: 条', '#FF9830')

row('App Server 诊断', 57)
graph('RPC 返回与排队耗时', 0, 62, 14,
      quantile('rpc', [('ms','返回',50), ('ms','返回',95), ('queue_ms','排队',95)]),
      ['返回 P50','返回 P95','排队 P95'], '分位数按每个时间桶内的已返回请求计算。', 'ms')
graph('JSON-RPC 返回量', 14, 62, 10,
      summed('rpc', [count_if('error = 0', '正常返回'), count_if('error = 1', '错误返回'),
                     count_if('timeout = 1', '返回里标记超时')]),
      ['正常返回','错误返回','返回里标记超时'], '正常返回表示该响应无 JSON-RPC 错误码。', bars=True)
graph('App Server 初始化与连接事件', 0, 70, 14,
      summed('connection', [count_if(f"event = '{e}'", title) for e, title in
                            [('init_ok','初始化成功'),('stop','正常停止'),('reconnect','尝试重连'),
                             ('disconnect','意外断开'),('init_error','初始化失败'),('timeout','超时')]]),
      ['初始化成功','正常停止','尝试重连','意外断开','初始化失败'], '只计桌面端记录到的状态变化和初始化结果。', bars=True)
table('最近 App Server 连接事件', 14, 70, 10, 8,
      f"""SELECT t/1000.0 AS time, CASE event WHEN 'init_ok' THEN '初始化成功' WHEN 'stop' THEN '正常停止'
      WHEN 'init_error' THEN '初始化失败' WHEN 'reconnect' THEN '尝试重连' WHEN 'disconnect' THEN '意外断开'
      WHEN 'timeout' THEN '超时' ELSE event END AS "事件", ms AS "初始化耗时"
      FROM connection WHERE {WINDOW} ORDER BY t DESC LIMIT 20""",
      '展示最近 20 条有日志的事件。', ['time'], ['初始化耗时'])

row('网络连接', 4)
up = [('ws_connect_error','WebSocket 连接失败'),('model_retry','模型采样重试'),
      ('prewarm_error','预热失败'),('http_fallback','回退 HTTP')]
graph('模型连接故障与回退', 10, 5, 14,
      summed('upstream', [count_if(f"event = '{e}'", title) for e, title in up]),
      [title for _, title in up], '上游异常和回退为日志条数。不同事件可能来自同一轮请求。', bars=True)
graph('Clash 拨号超时趋势', 0, 5, 10,
      summed('proxy', [count_if("cause='timeout' AND route='proxy'",'经代理超时'),
                       count_if("cause='timeout' AND route='direct'",'直连超时')]),
      ['经代理超时','直连超时'],
      '目标为 ChatGPT 或 OpenAI 域名，包含使用这些域名的其他应用流量。', bars=True)
table('上游异常原因', 0, 28, 24, 5,
      f"""SELECT CASE cause WHEN 'timeout' THEN '超时' WHEN 'refused' THEN '连接被拒绝'
      WHEN 'reset' THEN '连接重置' WHEN 'tls_handshake' THEN 'TLS / 握手' WHEN 'capacity' THEN '容量 / 429'
      WHEN 'dns' THEN 'DNS' ELSE '日志无明确原因' END AS "原因",
      {', '.join(count_if(f"event = '{e}'", title) for e, title in up if e != 'http_fallback')}
      FROM upstream WHERE {WINDOW} AND event != 'http_fallback' GROUP BY cause ORDER BY COUNT(*) DESC""",
      '按日志中明确的错误字符串分类。')

row('会话体验', 33)
graph('首 token、首次工具与总耗时', 0, 34, 14,
      quantile('turns', [('ttft_ms','首 token',95), ('tool_ms','首次工具',95), ('total_ms','总耗时',95)]),
      ['首 token P95','首次工具 P95','总耗时 P95'],
      '每条线使用该指标有记录的轮次。总耗时包含工具等待和已中断的轮次。', 'ms')
graph('轮次日志状态', 14, 34, 10,
      summed('turns', [count_if(f"status = '{e}'", title) for e, title in
                       [('recorded_end','有结束记录'),('interrupted','已中断'),('unknown','结束状态未观测')]]),
      ['有结束记录','已中断','结束状态未观测'], '横轴按轮次开始时间统计。', bars=True)
table('近期最慢的轮次', 0, 42, 24, 8,
      f"""SELECT t/1000.0 AS time, ttft_ms AS "首 token", tool_ms AS "首次工具",
      total_ms AS "总耗时", tools AS "工具调用",
      CASE status WHEN 'interrupted' THEN '已中断' ELSE '有结束记录' END AS "状态"
      FROM turns WHERE {WINDOW} AND total_ms IS NOT NULL ORDER BY total_ms DESC LIMIT 20""",
      '展示所选时段总耗时最高的 20 轮。', ['time'], ['首 token','首次工具','总耗时'])
table('数据来源与覆盖', 10, 51, 14, 6,
      """SELECT source AS "来源", status AS "读取状态", files AS "文件",
      last_write/1000.0 AS "最近写入", note AS "采样内容" FROM sources""",
      '显示最近一次扫描的读取情况。空白时段表示没有可用样本。', ['最近写入'])
table('实时采集状态', 0, 51, 10, 6,
      """SELECT captured_at/1000.0 AS "最近采集", window_start/1000.0 AS "回看起点",
      MAX(0, CAST(strftime('%s','now') AS INTEGER) - captured_at/1000) AS "数据延迟（秒）"
      FROM capture""",
      '显示采集器最近一次成功提交的时间。持续增加的延迟表示需要检查采集进程。',
      ['最近采集','回看起点'])
row('采集状态', 50)
stat('模型采样重试', 8,
     f"SELECT COUNT(*) AS value FROM upstream WHERE {WINDOW} AND event='model_retry'",
     '模型请求开始重试的日志条数。原因见下方的上游异常面板。', 'suffix: 条', '#FF9830')
stat('首次工具 · P95', 16, quantile('turns', [('tool_ms', '首次工具', 95)], False),
     '从一轮开始到首次工具启动，只有发出工具调用的轮次进入统计。', 'ms', '#73BF69')
node_timeline = graph(
    'codex 组节点观测与拨号超时', 0, 13, 24,
    f"""SELECT t/1000.0 AS time,
    MAX(CASE WHEN event='node_observed' THEN 2 END) AS "观测到不同节点",
    MAX(CASE WHEN event='timeout' THEN 1 END) AS "拨号超时"
    FROM clash_timeline WHERE {WINDOW} GROUP BY t ORDER BY t""",
    ['观测到不同节点', '拨号超时'],
    '蓝点是首次在连接日志里看到不同节点的时间，红点是 codex 组实际记录到的拨号超时。两条点列按同一时间轴对齐。',
)
node_timeline['fieldConfig']['defaults']['custom'].update(
    lineWidth=0, fillOpacity=0, showPoints='always', pointSize=8, axisPlacement='hidden',
)
node_timeline['fieldConfig']['overrides'][1]['properties'][0]['value']['fixedColor'] = '#F2495C'
table('codex 组节点与超时明细', 0, 21, 24, 7,
      f"""SELECT t/1000.0 AS "时间",
      CASE event WHEN 'node_observed' THEN '观测到不同节点' ELSE '拨号超时' END AS "事件",
      previous_node AS "上一观测节点", previous_t/1000.0 AS "上一观测时间",
      node AS "新观测节点",
      CASE cause WHEN 'connect_timeout' THEN '建立连接超时' WHEN 'dial_timeout' THEN '拨号超时' END AS "超时阶段"
      FROM clash_timeline WHERE {WINDOW} ORDER BY t DESC LIMIT 50""",
      '超时行的上一观测节点来自最近一条转发日志。节点变化行的时间以首次看到不同节点的连接为准。',
      ['时间', '上一观测时间'])
dns_row_y = 33
for panel in panels:
    if panel['gridPos']['y'] >= dns_row_y:
        panel['gridPos']['y'] += 17
row('DNS 解析与上游', dns_row_y)
dns_graph_fields = [
    count_if("event='resolve_error'", '域名解析失败'),
    count_if("event='upstream_error'", 'DNS 上游连接异常'),
    count_if("event='proxy_timeout'", 'codex 组拨号超时'),
]
graph('DNS 错误与代理超时', 0, 34, 14,
      f"""WITH events AS (
        SELECT t, event FROM dns_events WHERE {WINDOW}
        UNION ALL
        SELECT t, 'proxy_timeout' FROM clash_timeline WHERE {WINDOW} AND event='timeout'
      )
      SELECT {BUCKET}/1000 AS time, {', '.join(dns_graph_fields)}
      FROM events GROUP BY time ORDER BY time""",
      ['域名解析失败', 'DNS 上游连接异常', 'codex 组拨号超时'],
      '按时间分桶对照 Mihomo 日志。解析失败涵盖本机应用，上游取消单独显示在旁边的表里。', bars=True)
table('DNS 上游走向', 14, 34, 10, 8,
      f"""SELECT target AS "DNS 上游", COALESCE(route, '未记录') AS "出站",
      {count_if("event='upstream_routed'", '连接有记录')},
      {count_if("event='upstream_error'", '异常')},
      {count_if("event='upstream_canceled'", '取消')}
      FROM dns_events WHERE {WINDOW} AND event!='resolve_error'
      GROUP BY target, route ORDER BY COUNT(*) DESC LIMIT 25""",
      '按配置识别 DNS 上游，展示 Mihomo 记录的实际出站。连接与取消均以日志条数统计。')
table('DNS 日志明细', 0, 42, 24, 8,
      f"""SELECT strftime('%Y-%m-%dT%H:%M:%fZ', t/1000.0, 'unixepoch') AS "时间",
      CASE event WHEN 'resolve_error' THEN '域名解析失败' WHEN 'upstream_error' THEN '上游连接异常'
      WHEN 'upstream_canceled' THEN '上游连接取消' WHEN 'upstream_routed' THEN '上游连接有记录' END AS "阶段",
      target AS "DNS 上游 / 域名范围", route AS "出站", node AS "实际节点",
      CASE cause WHEN 'timeout' THEN '连接超时' WHEN 'dns' THEN '解析失败'
      WHEN 'refused' THEN '连接被拒绝' WHEN 'reset' THEN '连接被重置'
      WHEN 'tls_handshake' THEN 'TLS / 握手' WHEN 'canceled' THEN '取消'
      WHEN 'other' THEN '其他连接错误' END AS "原因"
      FROM dns_events WHERE {WINDOW} ORDER BY t DESC LIMIT 80""",
      '每行对应一条 Mihomo 日志。节点列仅在日志记录实际节点时展示。上游连接记录只证明到解析器的连接阶段。',
      ['时间'])
panels.sort(key=lambda panel: (panel['gridPos']['y'], panel['gridPos']['x']))

dashboard = {'id': None, 'uid': 'codex-health', 'title': 'Codex · 访问健康',
             'description': '本机桌面端、App Server、模型服务与 Clash 的日志观测。',
             'tags': ['codex','app-server','local'], 'timezone': 'browser', 'schemaVersion': 41,
             'version': 6, 'editable': True, 'refresh': '15s', 'time': {'from': 'now-3d', 'to': 'now'},
             'timepicker': {'refresh_intervals': ['15s','30s','1m','5m','15m'],
                            'time_options': ['1h','6h','24h','3d','7d']},
             'templating': {'list': []}, 'panels': panels}
Path(__file__).with_name('dashboard.json').write_text(json.dumps(dashboard, ensure_ascii=False, indent=2) + '\n')
