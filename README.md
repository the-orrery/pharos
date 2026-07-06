# pharos

本机基础设施健康监控:可插拔检查 → 有状态告警工单 → 多渠道通知。

像灯塔一样守着一台机器的本地服务(SSH 隧道、本地 HTTP 服务、launchd 任务、unix socket、状态文件等)——坏了就开一张告警工单,并按来源发到对应通知渠道。

## 模型

```
检查(checks.toml) ─run─▶ HealthEvent ─reconcile─▶ 告警工单 ─状态翻转─▶ 通知(按来源路由)
                                          firing / acked / resolved
```

- **检查 = 可插拔类型 + 配置**。下表 10 种检查类型,在 `checks.toml` 里实例化成对你机器的监控,不改代码。
- **告警 = 有状态工单,不是 fire-and-forget**。失败 → 开工单(通知);仍失败 → 只更新、不重复通知(去重);恢复 → 自动关单(通知)。你也可 `ack` / `resolve`。
- **通知 = 多渠道可插拔**。`console`(默认,无 secret)/ `dingtalk`(加签);按告警 `source` 路由到不同渠道。

## 快速开始

安装:`uv tool install` 本仓 → `pharos` 进 `PATH`(前置工具链见 [seed](https://github.com/the-orrery/seed))。

1. 写监控清单 `~/.config/pharos/checks.toml`(`examples/checks.toml` 是起手模板):

   ```toml
   [[check]]
   type = "HttpCheck"
   id = "api-healthz"
   source = "my-api"
   url = "http://127.0.0.1:8080/healthz"

   [[check]]
   type = "LaunchdCheck"
   id = "my-tunnel"
   source = "my-tunnel"
   label = "com.example.tunnel"
   require_running = true
   ```

2. 跑(定时运行可用 launchd、cron、Task Scheduler 等外部调度器):

   ```sh
   pharos run --config ~/.config/pharos/checks.toml                       # console 输出
   pharos run --config ~/.config/pharos/checks.toml --route channels.toml # 按来源路由通知
   ```

3. 看 / 处理告警工单:

   ```sh
   pharos alerts        # 当前在烧的(firing / acked)
   pharos alert <key>   # 详情 + runbook 指针
   pharos ack <key>     # 标"在处理"(停止重复通知)
   pharos resolve <key> # 手动关(检查自愈会自动关)
   ```

## 检查类型

通用类型(配置驱动,无硬编码目标):

| 类型 | 查什么 | 关键参数 |
|---|---|---|
| `CommandCheck` | 命令退出码 | `command` |
| `CommandJsonCheck` | 命令输出 JSON 的字段 == 期望值 | `command`, `success_field_path` |
| `HttpCheck` | HTTP GET/POST 状态码 | `url`, `method` |
| `LaunchdCheck` | launchd 任务 running/registered(可选输出含子串) | `label`, `require_running` |
| `PortOwnerCheck` | TCP 端口属主进程含某串 | `port`, `expected_owner_substring` |
| `UnixSocketPingCheck` | unix socket ping(可设"无进程即空闲=OK") | `socket_path` |
| `PidfileCheck` | pidfile 指向的进程存活且不陈旧 | `pidfile_path`, `stale_after_secs` |
| `FileContainsCheck` | 文件含必需子串 | `path`, `required_substrings` |
| `JsonStatusFileCheck` | JSON 状态文件的 state + 时间戳陈旧 | `status_file_path`, `state_field`, `stale_threshold_secs` |

contrib(领域专属,非通用):`SemanticSyncCheck`。每个类型的全部字段以 `src/pharos/checks/loader.py` 的 model 为准。

## 配置与 secret

- `checks.toml` — 监控清单(`--config`)。单条加 `enabled = false` 即关。
- `channels.toml` — 通知路由(`--route`):`channel → env_file` + `source → channel` + `default_channel`。
- **secret 边界**:webhook URL / 签名 secret **只在 `~/.config/<channel>.env`**(`DINGTALK_WEBHOOK_URL=` / `DINGTALK_SECRET=`),**绝不进仓**;`channels.toml` 只引用 env 文件路径。

## 开发

```sh
uv run poe check   # ruff + pyrefly + pytest
uv run poe fmt
```

- 怎么运作 / 为什么这么设计 → [`docs/overview.md`](docs/overview.md)
- 加自定义检查类型 / 通知渠道 → [`docs/architecture.md`](docs/architecture.md)

> 由 [seed](https://github.com/the-orrery/seed) 脚手架生成。同步模版演进：`seed update`。
