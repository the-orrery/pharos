---
description: "pharos 架构地图:本地健康检查、告警工单、通知路由的模块边界、核心不变量、run 数据流和改动入口。"
keywords: [pharos, architecture, checks, alerts, HealthEvent, reconcile, notify, routing, sqlite]
kind: reference
links: [overview]
code: [src/pharos/checks, src/pharos/alerts, src/pharos/notify, src/pharos/cli.py]
---

# pharos 架构

> 开发者地图。装 / 用见 [README](../README.md);本文给要改这块代码的人。

## 鸟瞰

pharos 周期性跑一组健康检查,把失败 reconcile 成有状态的告警工单,在状态翻转时通知。一条命令 `pharos run`:加载检查清单 → 逐项跑出 `HealthEvent` → 对工单库做增量对账 → 对新开 / 恢复的工单发通知。设计取舍(为什么告警是工单、为什么 generic/contrib 分层)见 overview。

## 模块地图

| 模块 | 职责 | 路径 |
|---|---|---|
| probes | 可复用探测原语(run_cmd / launchd / socket / http / pidfile / file-age / json) | `src/pharos/checks/probes.py` |
| check-types | 9 个通用参数化检查类型(配置驱动) | `src/pharos/checks/types.py` |
| contrib | 领域专属检查(非通用,如 `SemanticSyncCheck`) | `src/pharos/checks/contrib.py` |
| Check 协议 | `id` / `source` / `run() -> HealthEvent` | `src/pharos/checks/base.py` |
| loader | TOML → 检查实例(per-type pydantic 校验 + 类型注册表 `_REGISTRY`) | `src/pharos/checks/loader.py` |
| runner | 跑一组检查 + worst-of 聚合 | `src/pharos/checks/runner.py` |
| store | sqlite 工单库(firing / acked / resolved 生命周期) | `src/pharos/alerts/store.py` |
| manager | `reconcile`:HealthEvent → 工单状态翻转(NEW / ONGOING / RESOLVED) | `src/pharos/alerts/manager.py` |
| notify 核心 | `HealthEvent` / `Severity` / `Status` / `Channel` 协议 / `redact` | `src/pharos/notify/base.py` |
| 渠道后端 | `console` / `dingtalk` | `src/pharos/notify/console.py`, `dingtalk.py` |
| notify 调度 | 渠道注册表 `_BUILDERS` + best-effort 广播 | `src/pharos/notify/__init__.py` |
| routing | `source → channel` 路由(`channels.toml`) | `src/pharos/notify/routing.py` |
| cli | typer 命令(run / alerts / alert / ack / resolve / notify) | `src/pharos/cli.py` |

## 核心不变量

- **检查只读**:`Check.run()` 探测 + 返回 `HealthEvent`,不改被测系统(probes 全只读)。破坏 → 监控有副作用。
- **工单 key = `check.id`**,全局唯一稳定;reconcile 按它对账。两个检查同 `id` 会互相覆盖。
- **恢复 = 这次跑了且现在 `OK`**。缺席(本次没跑到)和 `UNKNOWN`(测不出)**不算恢复**,工单保留——否则子集运行会误清真告警。
- **状态翻转才通知**:`NEW` / `RESOLVED` 通知,`ONGOING` 不通知(= 去重 / silence,无独立 flag)。
- **secret 不出 `~/.config`**:webhook / secret 只在 `~/.config/*.env`,运行时由 `DingTalkChannel.from_env*` 读;代码 / 配置 / 仓里都没有。错误经 `redact` 脱敏再外抛。
- **通知 best-effort**:单渠道失败不掀翻一次 run。
- **通用 vs contrib 边界**:能参数化的进 `types.py`;需特殊状态语义的进 `contrib.py`。

## 数据流(`pharos run`)

`cli.run` → `loader.load_checks(checks.toml)` → `runner.run_checks` → 每个 `Check.run()` 出 `HealthEvent` → `manager.reconcile`(对 `store` 增量对账)→ `Transition[]` → 对 `NEW` / `RESOLVED`:`notify`(扁平 `--channels`)或 `Routing.notify`(`--route`,按 `source` 路由到 channel → `DingTalkChannel`)。退出码 = worst-of 聚合(`OK` 0 / `DEGRADED` 1 / `DOWN` 2)。

## 改 X 去哪

| 我想加 / 改 … | 从这里入手 | 坑 |
|---|---|---|
| 加一个通用检查类型 | `checks/types.py`(实现 `Check`)+ `checks/loader.py` 注册 model 进 `_REGISTRY` | 协议 = `id` / `source` / `run()->HealthEvent`;构造参数走 pydantic model |
| 加一个领域专属检查 | `checks/contrib.py` + loader 注册 | 仅当无法参数化时才落这,别污染通用核心 |
| 加一个探测原语 | `checks/probes.py` | stdlib-only;http 错误脱敏 |
| 加一个通知渠道 | `notify/`(实现 `Channel`:`name` / `send`)+ `notify/__init__.py` 的 `_BUILDERS` | 错误必须 `redact`,别漏 token |
| 改告警生命周期 / 去重 | `alerts/manager.py` 的 `reconcile` | 受上面「恢复」不变量约束 |
| 改 source→bot 路由 | `notify/routing.py` + `channels.toml` | 只放 env_file 路径,不嵌 secret |
| 加 CLI 命令 | `cli.py` | 经 `telemetry.run_instrumented` 包装 |

## 非目标

- **不做进程管理 / 自动修复** —— 只监控 + 告警,不重启、不改服务。
- **不做时序指标 / 历史曲线** —— 点状检查,不存时间序列。
- **不做通知传输底层** —— 实际投递委托给渠道后端(webhook 等);routing / 工单是策略层。
- **不存 secret**。
