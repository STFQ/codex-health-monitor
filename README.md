# Codex 访问健康

本机 Grafana 面板用于定位 Codex Desktop 等待、模型连接重试、Clash 拨号超时和 DNS 异常。采集器读取已有日志，把结构化事件写入 SQLite。默认回看最近 7 天，采集器与面板每 15 秒刷新。Grafana 在本机固定端口提供只读查看。

![Grafana 看板截图，展示 Codex 响应延迟与 Clash 拨号超时](docs/images/grafana-dashboard.png)

## 快速启动

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

启动命令会安装用户 LaunchAgent。每次登录 macOS 后，Grafana 和采集器都会自动启动。`bash service.sh stop` 会卸载启动项并保留数据。

后台默认使用 `~/Library/Application Support/Codex Health Grafana` 保存 Grafana、插件、SQLite 数据和日志。启动时发现 SQLite 插件缺少关键文件，会从已缓存的安装包补齐。`service.sh status` 会显示服务日志路径，`GRAFANA_HOME` 可以指向已有的 Grafana 安装目录。

## 从旧版临时目录迁移

旧版把 Grafana 自身的数据库和 Codex 采集库放在临时目录。安装了独立 SG 节点采集器的机器还会有一个 SG 历史库。

| 文件 | 内容 | 迁移方式 |
|---|---|---|
| 旧运行目录的 `data/grafana.db` | Grafana 仪表盘、数据源和设置 | 备份并复制 `data/` |
| 旧运行目录的 `app/codex-health.sqlite` | Codex 与 Mihomo 日志采集数据 | 备份并复制 `app/` |
| `~/Library/Application Support/sg-node-quality/quality.sqlite` | SG 节点的 HTTPS、TCP 等历史探测数据 | 保留原路径，用 SQLite 在线备份 |

### 1. 停止旧 Grafana 并备份

在新版仓库根目录执行。以下命令使用旧版默认目录。如果之前设置过 `GRAFANA_RUNTIME_DIR`，先把 `old` 改成实际路径。若旧库已经被临时目录清理，先从备份恢复缺失的数据库。

```sh
(
  set -eu
  umask 077
  old="$(getconf DARWIN_USER_TEMP_DIR)codex-health-grafana"
  new="$HOME/Library/Application Support/Codex Health Grafana"
  sg_db="$HOME/Library/Application Support/sg-node-quality/quality.sqlite"

  test -f "$old/data/grafana.db"
  test -f "$old/app/codex-health.sqlite"
  if [ -e "$new" ]; then
    echo "目标目录已存在，请先备份并核对该目录里的数据。"
    exit 1
  fi

  bash service.sh stop
  backup="$(mktemp -d "$HOME/Library/Application Support/codex-health-backup.XXXXXX")"
  echo "备份目录：$backup"
  ditto "$old" "$backup/runtime"
  if [ -f "$sg_db" ]; then
    sqlite3 -readonly "$sg_db" ".backup \"$backup/sg-quality.sqlite\""
    sqlite3 -readonly "$backup/sg-quality.sqlite" 'PRAGMA quick_check;'
  fi

  mkdir -m 700 "$new"
  for dir in data app dashboards live-studio-dashboards provisioning; do
    if [ -d "$backup/runtime/$dir" ]; then
      ditto "$backup/runtime/$dir" "$new/$dir"
    fi
  done
  sqlite3 -readonly "$new/data/grafana.db" 'PRAGMA quick_check;'
  sqlite3 -readonly "$new/app/codex-health.sqlite" 'PRAGMA quick_check;'
)
```

每次 `quick_check` 应返回 `ok`。SG 采集器可以继续运行，它仍向原位置的 `quality.sqlite` 写入。保留备份，直到确认新实例能查询迁移前的记录。

### 2. 检查自定义配置并启动

如果旧实例有自定义仪表盘或数据源，检查新目录中 `provisioning/` 的绝对路径。指向旧运行目录的仪表盘路径改为新目录下对应的位置。SG 数据源继续指向原来的 `sg-node-quality/quality.sqlite`。需要额外数据源插件时，在新实例中安装相应插件。启动脚本会重新生成 Codex 面板的默认配置，并安装 SQLite 插件。

```sh
GRAFANA_RUNTIME_DIR="$HOME/Library/Application Support/Codex Health Grafana" bash service.sh start
bash service.sh status
curl --fail --silent --show-error http://127.0.0.1:3308/api/health
```

状态应显示 `running`，健康接口应返回 `"database": "ok"`。打开 Grafana，把时间范围选到迁移前，确认旧记录可见。装了 SG 面板的机器还可执行下面的查询，核对最早采样时间，并在稍后复查最新采样时间是否继续前进。

```sh
sqlite3 -readonly "$HOME/Library/Application Support/sg-node-quality/quality.sqlite" \
  "SELECT count(*), datetime(min(t)/1000,'unixepoch','localtime'),
          datetime(max(t)/1000,'unixepoch','localtime') FROM https;"
```

### 3. 验证登录自启

退出登录后重新登录，或重启电脑并登录同一用户。**先直接访问原端口，暂时不要执行 `service.sh start`**，这样才能验证是 macOS 自动启动了服务。登录后运行：

```sh
plutil -lint "$HOME/Library/LaunchAgents/local.codex-health.grafana.plist"
launchctl print "gui/$(id -u)/local.codex-health.grafana"
curl --fail --silent --show-error http://127.0.0.1:3308/api/health
```

确认任务来自 `~/Library/LaunchAgents/`，状态为 `running`，健康接口正常响应。独立 SG 采集器已安装时，用 `launchctl print "gui/$(id -u)/local.sg-node-quality.collector"` 核对其运行状态，再打开 SG 面板确认旧数据可查且新样本仍在写入。

## 面板怎么读

- **网络连接**显示模型连接失败、HTTP 回退、Clash 拨号超时，以及 `codex` 组中首次观测到不同节点的时间。节点变化来自转发日志，精确的 `url-test` 切换需要额外的内核事件。
- **DNS 解析与上游**把域名解析失败、DNS 上游连接异常和代理拨号超时对齐。明细可以查解析器、出站组、实际节点和错误分类。上游连接取消单独统计。现有日志可以对齐连接阶段，完整 DNS 查询成功率和耗时需要逐查询事件。
- **会话体验**显示首 token、首次工具动作和总耗时，帮助定位 Thinking 发生在哪个阶段。
- **采集状态**显示最近一次写入和各日志来源的可用性。
- **App Server 诊断**显示桌面端的 JSON-RPC 耗时、返回错误和连接事件。RPC 返回错误率的分母是已收到响应的调用，包含后台文件和插件操作。

## 开发与验证

```sh
python3 -m unittest discover -s tests -v
python3 build_dashboard.py
bash -n run.sh service.sh
git diff --exit-code -- dashboard.json
```

`collect.py` 是采集入口。`build_dashboard.py` 生成面板 JSON，修改面板时提交两者。AI agent 可以从 [AGENTS.md](AGENTS.md) 开始，数据表和采样边界见 [架构说明](docs/architecture.md)。

现有 Grafana 实例也可以安装 [SQLite 数据源插件](https://grafana.com/grafana/plugins/frser-sqlite-datasource/)，将 UID 为 `codex-health-local` 的只读数据源指向采集器生成的数据库，再导入 `dashboard.json`。

数据库、真实日志和机器配置属于运行环境。示例验证使用临时目录和合成事件。
