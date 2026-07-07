from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from orrery_heartbeat import check_update

from pharos import telemetry
from pharos.logging_setup import setup_logging

app = typer.Typer(no_args_is_help=True, add_completion=False, help="pharos")

_ENV_CONFIG = "PHAROS_CONFIG"


def canonical_config_path() -> Path:
    """Resolve the canonical checks config: $PHAROS_CONFIG override,
    else ($XDG_CONFIG_HOME or ~/.config)/pharos/checks.toml.

    Mirrors store.db_path()'s XDG resolution so a bare `pharos run` has a
    stable, cwd-independent default instead of a relative path that only
    resolves inside the repo."""
    override = os.environ.get(_ENV_CONFIG)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pharos" / "checks.toml"


@app.callback()
def _root() -> None:
    """保留子命令名空间 (typer 单命令会塌缩, 加 callback 防止)。"""


@app.command()
def hello(name: str = "world") -> None:
    """示例命令。"""
    setup_logging()
    typer.echo(f"hello, {name}")


@app.command()
def stats() -> None:
    """本地用量统计: per-verb 调用次数 / p50·p95 耗时 / 错误率 (零网络, 见 telemetry.py)。"""
    typer.echo(telemetry.stats())


@app.command(name="notify")
def notify_cmd(
    title: str,
    detail: str = "",
    source: str = "manual",
    severity: str = "warning",
    channels: str = "console",
) -> None:
    """发一条通知到指定渠道(逗号分隔, 见 notify 子系统)。"""
    setup_logging()
    from pharos.notify import HealthEvent, Severity, Status
    from pharos.notify import notify as _notify

    event = HealthEvent(
        source=source,
        status=Status.UNKNOWN,
        severity=Severity(severity),
        title=title,
        detail=detail,
    )
    results = _notify(event, [c.strip() for c in channels.split(",") if c.strip()])
    for name, err in results.items():
        typer.echo(f"{name}: {'ok' if err is None else 'FAIL ' + err}")
    if any(err is not None for err in results.values()):
        raise typer.Exit(1)


@app.command(name="run")
def run_cmd(
    config: str | None = typer.Option(
        None,
        "--config",
        "-c",
        help="检查清单 TOML 路径。默认 $PHAROS_CONFIG 或 ~/.config/pharos/checks.toml。",
    ),
    channels: str = typer.Option(
        "console", help="NEW/RESOLVED 转变的通知渠道(逗号分隔)。"
    ),
    route: str | None = typer.Option(
        None,
        "--route",
        help="source→channel 路由 TOML 路径。设了则按来源路由到对应 channel, "
        "优先于 --channels。",
    ),
) -> None:
    """加载检查清单逐项执行, 把结果 reconcile 进有状态告警台账, 只在 NEW(首次失败)
    与 RESOLVED(恢复)时通知;ONGOING(持续失败/已 ack)静默, 去重落在台账里。

    打印每项摘要 + 本轮转变汇总 + 总体聚合状态。
    退出码: OK=0 / DEGRADED=1 / DOWN=2(总体聚合状态, 与告警状态无关)。
    """
    setup_logging()
    import time

    from pharos.alerts.manager import NOTIFY_KINDS, reconcile
    from pharos.checks.loader import CheckConfigError, load_checks
    from pharos.checks.runner import aggregate, run_checks
    from pharos.notify import Status
    from pharos.notify import notify as _notify
    from pharos.notify.routing import RoutingConfigError, load_routing

    # Resolve config: explicit --config wins; else default to the canonical
    # XDG config so a bare `pharos run` works without assuming the cwd is the
    # repo. On not-found, emit an in-band usage hint pointing at the form that
    # works (E-1) instead of dying on a relative default with no next step.
    config_path = Path(config).expanduser() if config else canonical_config_path()
    if not config_path.exists():
        canonical = canonical_config_path()
        typer.echo(f"config error: config file not found: {config_path}", err=True)
        if config_path == canonical:
            typer.echo(f"hint: create {canonical} or pass `--config <path>`", err=True)
        else:
            typer.echo(f"hint: try `pharos run --config {canonical}`", err=True)
        raise typer.Exit(2)

    try:
        checks = load_checks(str(config_path))
    except CheckConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(2) from exc

    routing = None
    if route is not None:
        try:
            routing = load_routing(route)
        except RoutingConfigError as exc:
            typer.echo(f"route config error: {exc}", err=True)
            raise typer.Exit(2) from exc

    events = run_checks(checks)
    channel_list = [c.strip() for c in channels.split(",") if c.strip()]

    for check, event in zip(checks, events, strict=True):
        typer.echo(
            f"[{event.status.value.upper():8}] {check.id:24} "
            f"{event.source} — {event.title}"
        )

    keyed_events = [
        (check.id, event) for check, event in zip(checks, events, strict=True)
    ]
    transitions = reconcile(keyed_events, now=time.time())

    # Notify only the actionable transitions: NEW pages, RESOLVED clears.
    # ONGOING (still failing / acked) is intentionally silent.
    # --route 优先: 按 event.source 路由到对的 bot; 否则平铺到 --channels。
    notified = 0
    routed_lines: list[str] = []
    for tr in transitions:
        if tr.kind not in NOTIFY_KINDS:
            continue
        if routing is not None:
            res = routing.notify(tr.event)
            for chan, err in res.items():
                routed_lines.append(
                    f"  {tr.kind:8} {tr.event.source} → {chan}: "
                    f"{'ok' if err is None else 'FAIL ' + err}"
                )
        else:
            _notify(tr.event, channel_list)
        notified += 1

    counts = {"NEW": 0, "ONGOING": 0, "RESOLVED": 0}
    for tr in transitions:
        counts[tr.kind] = counts.get(tr.kind, 0) + 1
    typer.echo(
        f"transitions: {counts['NEW']} new · {counts['ONGOING']} ongoing · "
        f"{counts['RESOLVED']} resolved · {notified} notified"
    )
    if routed_lines:
        typer.echo("routed:")
        for line in routed_lines:
            typer.echo(line)

    overall = aggregate(events)
    typer.echo(f"overall: {overall.value.upper()}")
    raise typer.Exit({Status.OK: 0, Status.DEGRADED: 1, Status.DOWN: 2}.get(overall, 1))


# ---------------------------------------------------------------------------
# Alert ticket commands — query/triage the stateful alert store.
# ---------------------------------------------------------------------------


_SECS_PER_MIN = 60
_SECS_PER_HOUR = 3600
_SECS_PER_DAY = 86400


def _fmt_age(seconds: float) -> str:
    """Compact human age, e.g. 45s / 12m / 3h / 2d."""
    s = int(seconds)
    if s < _SECS_PER_MIN:
        return f"{s}s"
    if s < _SECS_PER_HOUR:
        return f"{s // _SECS_PER_MIN}m"
    if s < _SECS_PER_DAY:
        return f"{s // _SECS_PER_HOUR}h"
    return f"{s // _SECS_PER_DAY}d"


@app.command(name="alerts")
def alerts_cmd(
    all_: bool = typer.Option(
        False, "--all", help="包含已 resolved 的历史告警(默认只列 active)。"
    ),
    json_out: bool = typer.Option(
        False, "--json", help="输出机器可读 JSON(供 surface hook 消费)。"
    ),
) -> None:
    """列出告警工单。默认只列 active(firing/acked);--all 含 resolved。"""
    import json as _json
    import time

    from pharos.alerts import store

    rows = store.list_all() if all_ else store.list_active()

    if json_out:
        typer.echo(_json.dumps(rows, ensure_ascii=False, default=str))
        return

    if not rows:
        typer.echo("no active alerts" if not all_ else "no alerts recorded")
        return

    now = time.time()
    typer.echo(f"{'STATUS':<8} {'KEY':<24} {'SEV':<8} {'AGE':>5} {'SEEN':>5}  TITLE")
    for a in rows:
        age = _fmt_age(now - float(a.get("first_seen") or now))
        typer.echo(
            f"{a['status']:<8} {a['key']:<24} {a['severity']:<8} "
            f"{age:>5} {a['occurrences']:>5}  {a['title']}"
        )


@app.command(name="alert")
def alert_cmd(key: str) -> None:
    """单个告警详情:生命周期时间线 + evidence + runbook。"""
    import json as _json

    from pharos.alerts import store

    a = store.get(key)
    if a is None:
        typer.echo(f"no such alert: {key}", err=True)
        raise typer.Exit(1)

    typer.echo(f"key:        {a['key']}")
    typer.echo(f"source:     {a['source']}")
    typer.echo(f"status:     {a['status']}")
    typer.echo(f"severity:   {a['severity']}")
    typer.echo(f"title:      {a['title']}")
    if a.get("detail"):
        typer.echo(f"detail:     {a['detail']}")
    typer.echo(f"first_seen: {a['first_seen']}")
    typer.echo(f"last_seen:  {a['last_seen']}")
    if a.get("resolved_at"):
        typer.echo(f"resolved:   {a['resolved_at']}")
    typer.echo(f"occurrences:{a['occurrences']}")
    if a.get("ack_note"):
        typer.echo(f"ack_note:   {a['ack_note']}")
    evidence = a.get("evidence") or {}
    runbook = evidence.get("runbook") if isinstance(evidence, dict) else ""
    if runbook:
        typer.echo(f"runbook:    {runbook}")
    typer.echo("evidence:")
    typer.echo(_json.dumps(evidence, ensure_ascii=False, indent=2, default=str))


@app.command(name="ack")
def ack_cmd(
    key: str,
    note: str = typer.Option("", "--note", help="ack 备注(谁在处理 / 处理进展)。"),
) -> None:
    """确认一个告警(firing→acked):停止 re-page, 但仍计入 active 视图。"""
    from pharos.alerts import store

    if store.ack(key, note):
        typer.echo(f"acked: {key}")
    else:
        typer.echo(f"no open alert to ack: {key}", err=True)
        raise typer.Exit(1)


@app.command(name="resolve")
def resolve_cmd(key: str) -> None:
    """手动 resolve 一个告警(firing/acked→resolved):从 active 视图移除。"""
    from pharos.alerts import store

    if store.resolve(key):
        typer.echo(f"resolved: {key}")
    else:
        typer.echo(f"no open alert to resolve: {key}", err=True)
        raise typer.Exit(1)


def run() -> None:
    check_update("pharos", "the-orrery/pharos")
    """Console-script entry: 在 per-invocation telemetry 捕获下跑 CLI。
    wrapper 负责 stdout/stderr 捕获 + exit-code 映射, 然后向本地 SQLite ledger 写一行
    ($PHAROS_TELEMETRY_OFF 或 DO_NOT_TRACK 关闭)。"""
    raise SystemExit(telemetry.run_instrumented(app, sys.argv[1:]))


if __name__ == "__main__":
    run()
