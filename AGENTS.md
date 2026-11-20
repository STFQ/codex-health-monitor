# Agent guide

这是 macOS 上的单机监控。数据流为 Codex 与 Mihomo 本地日志 → Python 采集器 → SQLite → Grafana。应用使用 Python 标准库，后台由 launchd 管理。

## 先读哪些文件

| 任务 | 入口 |
|---|---|
| 解析日志、调整采样、修改 SQLite 表 | `collect.py` |
| 改 Grafana 指标、布局和 SQL | `build_dashboard.py` |
| 修改前台启动、Grafana 安装与数据源配置 | `run.sh` |
| 修改 macOS 后台启动方式 | `service.sh` |
| 确认字段语义、时间单位和诊断边界 | `docs/architecture.md` |
| 复现 DNS 和节点日志的关键场景 | `tests/test_monitor.py` |

## 维护约定

- 兼容现有 SQLite 文件。新增表或字段时明确迁移路径，并验证旧库和空库均能启动。
- SQLite 内部时间为 UTC Unix 毫秒。Grafana SQL 的范围边界为毫秒，标记为时间字段的输出需转换为 Unix 秒或 RFC 3339。
- 保留已有面板 ID。修改 `build_dashboard.py` 后执行 `python3 build_dashboard.py`，提交重新生成的 `dashboard.json`。
- 保持采集器只读访问原始日志和 Clash 配置。可以用合成日志验证行为。数据库、凭据、订阅、真实日志和机器绝对路径留在运行环境。
- DNS 上游取消、连接异常和域名解析失败是三个事件。`using` 日志表示出站连接有记录，DNS 应答和单次查询耗时需要更细日志。
- 节点时间轴记录首次观测到不同节点的转发时间。超时明细的上一观测节点只提供上下文。

## 每次变更后运行

```sh
python3 -m unittest discover -s tests -v
python3 build_dashboard.py
bash -n run.sh service.sh
git diff --exit-code -- dashboard.json
```

SQL 面板变更可用临时库逐个执行 `targets[].queryText`，先用整数替换 `$__from` 与 `$__to`。接入本机实例时通过 Grafana `/api/ds/query` 检查插件的字段类型和时区表现。
