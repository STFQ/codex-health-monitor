# Codex 访问健康

本机 Grafana 面板用于定位 Codex Desktop 等待、模型连接重试、Clash 拨号超时和 DNS 异常。采集器读取已有日志，把结构化事件写入 SQLite，默认保留最近 7 天。面板初始展示 3 天，采集器与面板每 15 秒刷新。Grafana 在本机端口提供只读查看。

## Windows 快速启动

需要 Windows x64 和 Python 3.11.4 或更新版本。用 Git Bash 在仓库根目录执行：

```bash
python run_windows.py start
```

首次启动会下载并校验 Windows 版 Grafana OSS，安装 SQLite 数据源插件，并回填可读日志。用 `python run_windows.py status` 确认启动完成，再打开 [Codex 访问健康](http://127.0.0.1:3308/d/codex-health)。运行数据库、下载缓存和后台日志默认位于仓库内的 `.grafana-runtime`。

| 操作 | 命令 |
|---|---|
| 查看后台状态 | `python run_windows.py status` |
| 停止后台运行 | `python run_windows.py stop` |
| 在终端前台运行 | `python run_windows.py run` |
| 启用登录自启 | `python run_windows.py enable-autostart` |
| 查看自启状态 | `python run_windows.py autostart-status` |
| 关闭登录自启 | `python run_windows.py disable-autostart` |
| 改用本机端口 3309 | `python run_windows.py start --port 3309` |
| 单次采集到指定数据库 | `python collect.py --database codex-health.sqlite` |

Windows 启动只需当前用户权限。启用自启后，任务计划程序中的 **Codex Health Monitor** 会在当前 Windows 用户登录时启动监控。自启任务使用注册时指定的路径和端口，移动仓库或 Python 后需重新执行 `enable-autostart`。后台进程可以在关闭终端后继续运行。查看启动或采集详情可读 `.grafana-runtime/service.log`。可通过 `GRAFANA_HOME` 指向已有的 Grafana 安装，或通过 `GRAFANA_RUNTIME_DIR` 指定运行目录。

| 来源 | 默认路径 |
|---|---|
| Codex Desktop | `%LOCALAPPDATA%/Codex/Logs` 和 Microsoft Store 应用包下的 `LocalCache/Local/Codex/Logs` |
| Codex 会话和运行时 | `%USERPROFILE%/.codex`，支持 `CODEX_HOME` |
| Mihomo Party | `%APPDATA%/mihomo-party/logs` |

`run_windows.py` 和 `collect.py` 都支持 `--desktop`、`--codex-home` 和 `--clash` 覆盖日志路径。未安装 Mihomo Party 时，面板仍可显示 Codex 数据。

## macOS 快速启动

需要 macOS 和 Python 3.11 或更新版本。在仓库根目录执行：

```sh
bash service.sh start
```

打开 [Codex 访问健康](http://127.0.0.1:3308/d/codex-health)。首次启动会下载并校验 Grafana OSS，安装 SQLite 数据源插件，并从可用日志回填数据。启动时间取决于下载速度和历史日志大小。没有安装 Clash Party 的机器仍然可以查看 Codex 数据。

| 操作 | 命令 |
|---|---|
| 查看后台状态 | `bash service.sh status` |
| 停止后台服务 | `bash service.sh stop` |
| 在终端前台运行 | `bash run.sh` |
| 改用本机端口 3309 | `GRAFANA_PORT=3309 bash service.sh start` |
| 单次采集到指定数据库 | `python3 collect.py --database codex-health.sqlite` |

后台服务依赖当前 macOS 登录会话，可在关闭 Codex 和终端后继续运行。退出登录或重启后，再执行启动命令。后台运行目录默认位于该用户的 macOS 私有临时目录，`service.sh status` 会显示服务日志路径。`GRAFANA_HOME` 可以指向已有的 Grafana 安装目录。

## 面板怎么读

- **网络连接**显示模型连接失败、HTTP 回退、Clash 拨号超时，以及 `codex` 组中首次观测到不同节点的时间。节点变化来自转发日志，精确的 `url-test` 切换需要额外的内核事件。
- **DNS 解析与上游**把域名解析失败、DNS 上游连接异常和代理拨号超时对齐。明细可以查解析器、出站组、实际节点和错误分类。上游连接取消单独统计。现有日志可以对齐连接阶段，完整 DNS 查询成功率和耗时需要逐查询事件。
- **会话体验**显示首 token、首次工具动作和总耗时，帮助定位 Thinking 发生在哪个阶段。
- **采集状态**显示最近一次写入和各日志来源的可用性。
- **App Server 诊断**显示桌面端的 JSON-RPC 耗时、返回错误和连接事件。RPC 返回错误率的分母是已收到响应的调用，包含后台文件和插件操作。

## 开发与验证

```sh
python -m unittest discover -s tests -v
python build_dashboard.py
bash -n run.sh && bash -n service.sh
git diff --exit-code -- dashboard.json
```

`collect.py` 是采集入口。`build_dashboard.py` 生成面板 JSON，修改面板时提交两者。macOS 上也可用 `python3` 执行上述 Python 命令。AI agent 可以从 [AGENTS.md](AGENTS.md) 开始，数据表和采样边界见 [架构说明](docs/architecture.md)。

现有 Grafana 实例也可以安装 [SQLite 数据源插件](https://grafana.com/grafana/plugins/frser-sqlite-datasource/)，将 UID 为 `codex-health-local` 的只读数据源指向采集器生成的数据库，再导入 `dashboard.json`。

数据库、真实日志和机器配置属于运行环境。示例验证使用临时目录和合成事件。
