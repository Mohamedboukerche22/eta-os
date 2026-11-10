"""``eta-admin``: the master administrator command-line interface.

Privileged actions never run directly from this process. They are delegated to
the root-owned ``eta-priv`` helper through sudo, which authenticates with PAM.
Read-only status commands work for any local user; sensitive logs require
privilege.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import IDENTITY_LINE, PRODUCT, UPSTREAM, __version__
from .auth import AuthUnavailable, current_user
from .backup import BackupManager
from .config import Settings, load_settings
from .health import HealthResult, collect_health, overall_state, wait_for
from .logger import EtaLogger, read_log, tail_file
from .maintenance import MaintenanceSession
from .services import (
    PROTECTED,
    REGISTRY,
    Systemd,
    get_spec,
    services_report,
    validate_service_key,
)
from .util import CommandResult, EtaError, Runner, format_duration, human_bytes, read_json, run, uptime_seconds

LOG_COMPONENTS = {
    "boot", "services", "backend", "database", "backup", "security",
    "errors", "recovery", "monitor", "admin", "install",
}


class PermissionDenied(EtaError):
    pass


class PrivilegedExecutor:
    """Runs eta-priv via sudo. Root can call it directly."""

    def __init__(
        self,
        *,
        priv_bin: str | None = None,
        sudo: str | None = None,
        runner: Runner = run,
        euid: int | None = None,
    ) -> None:
        self.priv_bin = priv_bin or shutil.which("eta-priv") or "/run/current-system/sw/bin/eta-priv"
        self.sudo = sudo or shutil.which("sudo") or "/run/wrappers/bin/sudo"
        self.runner = runner
        self.euid = os.geteuid() if euid is None else euid

    @property
    def is_root(self) -> bool:
        return self.euid == 0

    def authenticate(self, *, interactive: bool = True) -> bool:
        if self.is_root:
            return True
        if not interactive:
            return False
        try:
            proc = subprocess.run([self.sudo, "-v"], check=False)
            return proc.returncode == 0
        except OSError:
            return False

    def run(self, args: Sequence[str], *, timeout: float = 600.0) -> CommandResult:
        if self.is_root:
            argv = [self.priv_bin, *args]
        else:
            argv = [self.sudo, "-n", self.priv_bin, *args]
        result = self.runner(argv, timeout=timeout)
        lowered = (result.stderr or "").lower()
        if "password is required" in lowered or "a terminal is required" in lowered:
            raise PermissionDenied("sudo authentication required; run 'sudo -v' or re-run interactively")
        return result


@dataclass
class AdminContext:
    settings: Settings
    logger: EtaLogger
    security_logger: EtaLogger
    systemd: Systemd
    executor: PrivilegedExecutor
    json_output: bool = False
    assume_yes: bool = False
    interactive: bool = True
    clock: Callable[[], float] = time.time
    input_func: Callable[[str], str] = input
    output: Any = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def print(self, text: str = "") -> None:
        if self.output is not None:
            print(text, file=self.output)
        else:
            print(text)

    def confirm(self, prompt: str, *, token: str | None = None) -> bool:
        if self.assume_yes:
            return True
        if not self.interactive:
            raise EtaError("confirmation required; use --yes for non-interactive operation")
        answer = self.input_func(prompt).strip()
        if token is not None:
            return answer == token
        return answer.lower() in {"y", "yes"}


def require_privilege(ctx: AdminContext, *, interactive: bool | None = None) -> None:
    if ctx.executor.is_root:
        return
    use_interactive = ctx.interactive if interactive is None else interactive
    if not ctx.executor.authenticate(interactive=use_interactive):
        raise PermissionDenied(
            "master authentication required (sudo/PAM). No privileged command was executed."
        )


# -- reports ----------------------------------------------------------------
def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, rest = line.split(":", 1)
                values[key] = int(rest.strip().split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return values


def _loadavg() -> list[float]:
    try:
        return [float(value) for value in Path("/proc/loadavg").read_text().split()[:3]]
    except (OSError, ValueError):
        return []


def _process_count() -> int:
    try:
        return len([name for name in os.listdir("/proc") if name.isdigit()])
    except OSError:
        return 0


def system_status(ctx: AdminContext) -> dict[str, Any]:
    settings = ctx.settings
    health = read_json(settings.run_dir / "health.json", {})
    boot = read_json(settings.run_dir / "boot-summary.json", {})
    backup = read_json(settings.get("backup.status_file"), {})
    mem = _meminfo()
    try:
        disk = shutil.disk_usage("/")
        disk_data = {"total": disk.total, "used": disk.used, "free": disk.free}
    except OSError:
        disk_data = {}
    return {
        "schema": "eta.status.v1",
        "eta_version": __version__,
        "upstream": UPSTREAM,
        "creator": "mohamedboukerch55",
        "hostname": os.uname().nodename,
        "kernel": f"{os.uname().sysname} {os.uname().release}",
        "uptime_seconds": round(uptime_seconds(ctx.clock), 1),
        "systemd": ctx.systemd.overall_state(),
        "failed_units": ctx.systemd.failed_units(),
        "health": health,
        "overall": health.get("overall", "unknown") if isinstance(health, dict) else "unknown",
        "boot": boot,
        "memory": mem,
        "disk": disk_data,
        "services": services_report(settings, ctx.systemd),
        "backup": backup if isinstance(backup, dict) else {},
        "database_engine": settings.db_engine,
        "master_user": settings.master_user,
    }


def resource_report(ctx: AdminContext) -> dict[str, Any]:
    mem = _meminfo()
    total = mem.get("MemTotal", 0)
    available = mem.get("MemAvailable", 0)
    services = services_report(ctx.settings, ctx.systemd)
    service_memory = sum((row.get("memory_bytes") or 0) for row in services)
    try:
        disk = shutil.disk_usage("/")
        disk_data = {"total": disk.total, "used": disk.used, "free": disk.free}
    except OSError:
        disk_data = {}
    return {
        "schema": "eta.resources.v1",
        "memory_total_bytes": total,
        "memory_available_bytes": available,
        "memory_used_percent": round((total - available) / total * 100, 1) if total else None,
        "swap_total_bytes": mem.get("SwapTotal", 0),
        "swap_free_bytes": mem.get("SwapFree", 0),
        "loadavg": _loadavg(),
        "processes": _process_count(),
        "uptime_seconds": round(uptime_seconds(ctx.clock), 1),
        "disk": disk_data,
        "service_memory_bytes": service_memory,
        "services": [
            {
                "key": row["key"],
                "unit": row["unit"],
                "active": row["active"],
                "memory_bytes": row["memory_bytes"],
                "restarts": row["restarts"],
            }
            for row in services
        ],
    }


def doctor_report(ctx: AdminContext) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def add(level: str, name: str, detail: str) -> None:
        checks.append({"level": level, "check": name, "detail": detail})

    add("ok", "configuration", f"loaded; database={ctx.settings.db_engine}")
    for label, path in (
        ("log directory", ctx.settings.log_dir),
        ("state directory", ctx.settings.state_dir),
        ("runtime directory", ctx.settings.run_dir),
    ):
        try:
            mode = os.stat(path).st_mode & 0o777
            if mode & 0o002:
                add("warn", label, f"{path} is world-writable ({oct(mode)})")
            else:
                add("ok", label, f"{path} ({oct(mode)})")
        except OSError:
            add("warn", label, f"{path} missing")
    key = Path(str(ctx.settings.get("backup.password_file")))
    if key.exists():
        mode = key.stat().st_mode & 0o777
        if mode & 0o077:
            add("critical", "restic key", f"{key} permissions {oct(mode)} (expected 0600)")
        else:
            add("ok", "restic key", f"{key} ({oct(mode)})")
    else:
        add("warn", "restic key", f"{key} missing (backups cannot run)")
    etc_eta = Path(str(ctx.settings.get("config_dir", "/etc/eta")))
    try:
        mode = os.stat(etc_eta).st_mode & 0o777
        if mode & 0o002:
            add("critical", "configuration", f"{etc_eta} is world-writable")
        else:
            add("ok", "configuration perms", f"{etc_eta} ({oct(mode)})")
    except OSError:
        pass
    failed = ctx.systemd.failed_units()
    if failed:
        add("warn", "failed units", ", ".join(failed))
    else:
        add("ok", "failed units", "none")
    try:
        disk = shutil.disk_usage("/")
        percent = disk.free / disk.total * 100
        level = "ok" if percent >= 10 else ("warn" if percent >= 5 else "critical")
        add(level, "disk space", f"{human_bytes(disk.free)} free ({percent:.1f}%)")
    except OSError:
        pass
    if ctx.settings.enabled("ssh"):
        add("warn", "ssh", "SSH administration is enabled; restrict it to the LAN")
    else:
        add("ok", "ssh", "disabled by default")
    firewall = ctx.systemd.show("firewall.service")
    add("ok" if firewall.active else "warn", "firewall", f"firewall.service {firewall.summary()}")
    # Reject secrets in settings (only *_file references are allowed).
    def scan(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                scan(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, str) and "password" in path.lower() and not path.endswith("_file"):
            add("critical", "secret in settings", f"plaintext value at {path}")

    scan(ctx.settings.raw)
    return checks


# -- renderers --------------------------------------------------------------
def _print_status(ctx: AdminContext, payload: Mapping[str, Any]) -> None:
    ctx.print(f"{PRODUCT} {__version__}  |  {UPSTREAM}")
    ctx.print(IDENTITY_LINE)
    ctx.print("")
    ctx.print(f"hostname:   {payload['hostname']}")
    ctx.print(f"kernel:     {payload['kernel']}")
    ctx.print(f"uptime:     {format_duration(payload['uptime_seconds'])}")
    ctx.print(f"systemd:    {payload['systemd']}")
    ctx.print(f"database:   {payload['database_engine']}")
    ctx.print(f"master:     {payload['master_user']}")
    mem = payload.get("memory") or {}
    if mem:
        ctx.print(
            f"memory:     {human_bytes(mem.get('MemAvailable', 0))} available / "
            f"{human_bytes(mem.get('MemTotal', 0))} total"
        )
    disk = payload.get("disk") or {}
    if disk:
        ctx.print(f"disk:       {human_bytes(disk.get('free', 0))} free / {human_bytes(disk.get('total', 0))} total")
    ctx.print(f"failed:     {', '.join(payload['failed_units']) if payload['failed_units'] else 'none'}")
    ctx.print("")
    ctx.print(f"OVERALL: {str(payload['overall']).upper()}")
    health = payload.get("health") or {}
    services = health.get("services") if isinstance(health, dict) else {}
    if isinstance(services, dict):
        for key, value in sorted(services.items()):
            marker = "OK" if value.get("ok") else ("SKIP" if value.get("status") == "skipped" else "FAIL")
            ctx.print(f"  [{marker}] {key}: {value.get('detail')}")
    backup = payload.get("backup") or {}
    if backup:
        ctx.print(
            f"backup:     {backup.get('outcome', 'unknown')} "
            f"{backup.get('last_success', {}).get('last_end', '') if isinstance(backup.get('last_success'), dict) else ''}"
        )
    boot = payload.get("boot") or {}
    if boot.get("duration_seconds") is not None:
        ctx.print(f"last boot:  {format_duration(boot['duration_seconds'])} ({boot.get('overall')})")


def _print_services(ctx: AdminContext, rows: list[dict[str, Any]]) -> None:
    ctx.print(f"{'SERVICE':12} {'STATE':12} {'ENABLED':10} {'HEALTH':10} {'RESTARTS':8} {'MEMORY':10} UNIT")
    health = read_json(ctx.settings.run_dir / "health.json", {})
    health_services = health.get("services", {}) if isinstance(health, dict) else {}
    for row in rows:
        h = health_services.get(row["key"], {})
        health_text = "ok" if h.get("ok") else ("skip" if h.get("status") == "skipped" else ("fail" if h else "?"))
        mem = human_bytes(row["memory_bytes"]) if row.get("memory_bytes") is not None else "-"
        ctx.print(
            f"{row['key']:12} {(row['active'] and 'active' or row['sub_state']):12} "
            f"{('yes' if row['enabled'] else 'no'):10} {health_text:10} "
            f"{str(row.get('restarts', 0)):8} {mem:10} {row['unit']}"
        )


def _print_logs(records: Sequence[Mapping[str, Any]]) -> None:
    for record in records:
        print(
            f"{record.get('ts')} {str(record.get('severity', '')).upper():8} "
            f"{record.get('component')}: {record.get('message')}"
        )


# -- commands ---------------------------------------------------------------
def cmd_status(ctx: AdminContext) -> int:
    payload = system_status(ctx)
    if ctx.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        _print_status(ctx, payload)
    return 0


def cmd_services(ctx: AdminContext) -> int:
    rows = services_report(ctx.settings, ctx.systemd)
    if ctx.json_output:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        _print_services(ctx, rows)
    return 0


def cmd_logs(ctx: AdminContext, component: str, lines: int, journal: str | None = None) -> int:
    if component not in LOG_COMPONENTS:
        raise EtaError(f"unknown log component: {component}")
    if component in {"security", "admin"} and not ctx.executor.is_root:
        require_privilege(ctx)
        result = ctx.executor.run(["logs", component, str(lines)], timeout=60)
        if result.stdout:
            print(result.stdout, end="")
        return result.returncode
    if journal:
        unit = journal
        result = ctx.executor.run(["journal", unit, str(lines)], timeout=60)
        if result.stdout:
            print(result.stdout, end="")
        return result.returncode
    try:
        records = read_log(component, lines=lines, log_dir=ctx.settings.log_dir)
    except PermissionError:
        require_privilege(ctx)
        result = ctx.executor.run(["logs", component, str(lines)], timeout=60)
        if result.stdout:
            print(result.stdout, end="")
        return result.returncode
    if ctx.json_output:
        print(json.dumps(records, indent=2, sort_keys=True))
    else:
        _print_logs(records)
    return 0


def cmd_backup(ctx: AdminContext, action: str, *, snapshot: str = "latest", target: str | None = None, dry_run: bool = False, overwrite: bool = False) -> int:
    manager = BackupManager(ctx.settings)
    if action == "status":
        payload = manager.status()
        if ctx.json_output:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            print(f"outcome:    {payload.get('outcome')}")
            print(f"repository: {payload.get('repository')} (exists={payload.get('repository_exists')})")
            print(f"running:    {payload.get('running')}")
            if payload.get("last_success"):
                print(f"last success: {payload['last_success'].get('last_end')} {payload['last_success'].get('snapshot_id') or ''}")
            if payload.get("error"):
                print(f"error:      {payload['error']}")
        return 0
    require_privilege(ctx)
    if action == "list":
        result = ctx.executor.run(["backup", "list"], timeout=120)
        print(result.stdout, end="")
        return result.returncode
    if action == "run":
        if not ctx.confirm("Run a backup now? [y/N] "):
            return 1
        result = ctx.executor.run(["backup", "run"], timeout=6 * 3600)
        print(result.stdout or result.stderr, end="")
        return result.returncode
    if action == "restore":
        if not ctx.confirm(
            f"Restore snapshot {snapshot} to {target or manager.restore_root}? Type RESTORE: ",
            token="RESTORE",
        ):
            print("Restore cancelled.")
            return 1
        argv = ["backup", "restore", "--snapshot", snapshot, "--confirm"]
        if target:
            argv.extend(["--target", target])
        if dry_run:
            argv.append("--dry-run")
        if overwrite:
            argv.append("--overwrite")
        result = ctx.executor.run(argv, timeout=6 * 3600)
        print(result.stdout or result.stderr, end="")
        return result.returncode
    raise EtaError(f"unknown backup action: {action}")


def cmd_restart(ctx: AdminContext, service: str) -> int:
    key = validate_service_key(service)
    spec = get_spec(key)
    require_privilege(ctx)
    if not ctx.confirm(f"Restart {spec.title} ({spec.unit})? [y/N] "):
        return 1
    # A manual restart is an explicit new recovery budget.
    ctx.executor.run(["reset-recovery", key], timeout=30)
    result = ctx.executor.run(["systemctl", "restart", spec.unit], timeout=120)
    if result.returncode != 0:
        print(result.stdout or result.stderr, end="")
        return result.returncode
    results = wait_for(ctx.settings, [key], timeout=45)
    health = results.get(key)
    ok = bool(health and health.ok)
    print(f"{key}: {'healthy' if ok else 'not healthy'} - {health.detail if health else 'no result'}")
    ctx.logger.info(
        "Administrator restarted service",
        event="admin_restart",
        service=key,
        ok=ok,
        user=current_user(),
    )
    return 0 if ok else 4


def cmd_toggle(ctx: AdminContext, service: str, enable: bool, *, force: bool = False) -> int:
    key = validate_service_key(service)
    spec = get_spec(key)
    if not enable and key in PROTECTED and not force:
        raise EtaError(f"{key} is a protected core service; use --force only if you understand the risk")
    require_privilege(ctx)
    verb = "enable" if enable else "disable"
    if not ctx.confirm(f"{verb.capitalize()} {spec.title} ({spec.unit})? [y/N] "):
        return 1
    result = ctx.executor.run(["systemctl", verb, spec.unit], timeout=120)
    print(result.stdout or result.stderr, end="")
    ctx.logger.info(
        "Administrator changed service enablement",
        event="admin_enable" if enable else "admin_disable",
        service=key,
        user=current_user(),
        exit_code=result.returncode,
    )
    if result.returncode != 0:
        return result.returncode
    if enable:
        results = wait_for(ctx.settings, [key], timeout=45)
        health = results.get(key)
        print(f"{key}: {'healthy' if health and health.ok else 'not healthy'} - {health.detail if health else 'no result'}")
    return 0


def cmd_install(ctx: AdminContext, service: str) -> int:
    if not service or not all(ch.isalnum() or ch == "-" for ch in service):
        raise EtaError("invalid service name")
    require_privilege(ctx)
    if not ctx.confirm(f"Install/enable offline service {service}? [y/N] "):
        return 1
    result = ctx.executor.run(["install-service", service], timeout=300)
    print(result.stdout or result.stderr, end="")
    ctx.logger.info("Administrator installed service", event="admin_install", service=service, exit_code=result.returncode)
    if result.returncode != 0:
        return result.returncode
    if service in REGISTRY:
        results = wait_for(ctx.settings, [service], timeout=45)
        health = results.get(service)
        print(f"{service}: {'healthy' if health and health.ok else 'not healthy'} - {health.detail if health else 'no result'}")
    return 0


def cmd_update(ctx: AdminContext) -> int:
    require_privilege(ctx)
    source = Path("/etc/eta/source/flake.nix")
    if not source.is_file():
        raise EtaError("offline update source /etc/eta/source is missing")
    print(f"Offline update source: {source}")
    print("Network access is not used. The update is built from the local store only.")
    if not ctx.confirm("Apply the offline system update? Type UPDATE: ", token="UPDATE"):
        print("Update cancelled.")
        return 1
    result = ctx.executor.run(["update"], timeout=3600)
    print(result.stdout or result.stderr, end="")
    ctx.logger.info("Administrator applied offline update", event="admin_update", exit_code=result.returncode)
    return result.returncode


def cmd_reset(ctx: AdminContext, service: str) -> int:
    key = validate_service_key(service)
    require_privilege(ctx)
    result = ctx.executor.run(["reset-recovery", key], timeout=30)
    print(result.stdout or result.stderr, end="")
    return result.returncode


def cmd_maintenance(ctx: AdminContext) -> int:
    require_privilege(ctx)
    session = MaintenanceSession(
        ctx.settings,
        logger=ctx.logger,
        systemd=ctx.systemd,
        terminal=ctx.output or sys.stdout,
        input_func=ctx.input_func,
    )
    ctx.logger.security("Administrator entered maintenance mode", event="admin_maintenance", user=current_user())
    return session.run()


def cmd_doctor(ctx: AdminContext) -> int:
    checks = doctor_report(ctx)
    if ctx.json_output:
        print(json.dumps(checks, indent=2, sort_keys=True))
    else:
        for check in checks:
            print(f"[{check['level'].upper():8}] {check['check']}: {check['detail']}")
    critical = [check for check in checks if check["level"] == "critical"]
    return 1 if critical else 0


def cmd_resources(ctx: AdminContext) -> int:
    payload = resource_report(ctx)
    if ctx.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"memory:     {human_bytes(payload['memory_available_bytes'])} available / {human_bytes(payload['memory_total_bytes'])}")
        print(f"swap:       {human_bytes(payload['swap_free_bytes'])} free / {human_bytes(payload['swap_total_bytes'])}")
        print(f"loadavg:    {' '.join(str(v) for v in payload['loadavg']) or 'n/a'}")
        print(f"processes:  {payload['processes']}")
        print(f"uptime:     {format_duration(payload['uptime_seconds'])}")
        disk = payload.get("disk") or {}
        if disk:
            print(f"disk:       {human_bytes(disk.get('free', 0))} free / {human_bytes(disk.get('total', 0))}")
        print(f"services:   {human_bytes(payload['service_memory_bytes'])} reported by systemd")
        for row in payload["services"]:
            mem = human_bytes(row["memory_bytes"]) if row.get("memory_bytes") is not None else "-"
            print(f"  {row['key']:12} {'active' if row['active'] else 'inactive':10} {mem}")
    return 0


def cmd_version(ctx: AdminContext) -> int:
    print(f"{PRODUCT} {__version__}")
    print(f"Upstream: {UPSTREAM}")
    print(IDENTITY_LINE)
    print("License: MIT (project), MIT (NixOS/nixpkgs)")
    return 0


# -- CLI --------------------------------------------------------------------
def _extract_global(argv: list[str]) -> tuple[list[str], bool, bool, bool]:
    rest: list[str] = []
    json_output = False
    assume_yes = False
    non_interactive = False
    for arg in argv:
        if arg == "--json":
            json_output = True
        elif arg in {"--yes", "-y"}:
            assume_yes = True
        elif arg == "--non-interactive":
            non_interactive = True
        else:
            rest.append(arg)
    return rest, json_output, assume_yes, non_interactive


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eta-admin",
        description=f"{PRODUCT} master administration tool. {IDENTITY_LINE}",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="system status and health")
    sub.add_parser("services", help="list services")
    logs = sub.add_parser("logs", help="read persistent logs")
    logs.add_argument("component", nargs="?", default="errors")
    logs.add_argument("-n", "--lines", type=int, default=100)
    logs.add_argument("--journal", metavar="UNIT", default=None)
    backup = sub.add_parser("backup", help="backup operations")
    backup_sub = backup.add_subparsers(dest="backup_action", required=True)
    backup_sub.add_parser("status")
    backup_sub.add_parser("run")
    backup_sub.add_parser("list")
    restore = backup_sub.add_parser("restore")
    restore.add_argument("--snapshot", default="latest")
    restore.add_argument("--target")
    restore.add_argument("--dry-run", action="store_true")
    restore.add_argument("--overwrite", action="store_true")
    top_restore = sub.add_parser("restore", help="restore a backup (alias)")
    top_restore.add_argument("--snapshot", default="latest")
    top_restore.add_argument("--target")
    top_restore.add_argument("--dry-run", action="store_true")
    top_restore.add_argument("--overwrite", action="store_true")
    sub.add_parser("maintenance", help="enter the authenticated maintenance environment")
    restart = sub.add_parser("restart", help="restart a service")
    restart.add_argument("service")
    enable = sub.add_parser("enable", help="enable and start a service")
    enable.add_argument("service")
    enable.add_argument("--force", action="store_true")
    disable = sub.add_parser("disable", help="stop and disable a service")
    disable.add_argument("service")
    disable.add_argument("--force", action="store_true")
    install = sub.add_parser("install", help="install/enable a service from the offline catalog")
    install.add_argument("service")
    sub.add_parser("update", help="apply an offline system update from /etc/eta/source")
    reset = sub.add_parser("reset", help="reset the bounded recovery budget for a service")
    reset.add_argument("service")
    sub.add_parser("doctor", help="check permissions, failed units and disk")
    sub.add_parser("resources", help="memory, CPU, disk and service resource report")
    sub.add_parser("version", help="print version and identity")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    cleaned, json_output, assume_yes, non_interactive = _extract_global(raw)
    parser = build_parser()
    args = parser.parse_args(cleaned)
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"eta-admin: configuration error: {exc}", file=sys.stderr)
        return 2
    logger = EtaLogger("admin", settings.log_dir, run_dir=settings.run_dir)
    security = EtaLogger("security", settings.log_dir, run_dir=settings.run_dir)
    interactive = sys.stdin.isatty() and not non_interactive
    ctx = AdminContext(
        settings=settings,
        logger=logger,
        security_logger=security,
        systemd=Systemd(),
        executor=PrivilegedExecutor(),
        json_output=json_output,
        assume_yes=assume_yes,
        interactive=interactive,
    )
    try:
        if args.command == "status":
            return cmd_status(ctx)
        if args.command == "services":
            return cmd_services(ctx)
        if args.command == "logs":
            return cmd_logs(ctx, args.component, max(1, min(args.lines, 5000)), args.journal)
        if args.command == "backup":
            action = args.backup_action
            if action == "restore":
                return cmd_backup(ctx, "restore", snapshot=args.snapshot, target=args.target, dry_run=args.dry_run, overwrite=args.overwrite)
            return cmd_backup(ctx, action)
        if args.command == "restore":
            return cmd_backup(ctx, "restore", snapshot=args.snapshot, target=args.target, dry_run=args.dry_run, overwrite=args.overwrite)
        if args.command == "maintenance":
            return cmd_maintenance(ctx)
        if args.command == "restart":
            return cmd_restart(ctx, args.service)
        if args.command == "enable":
            return cmd_toggle(ctx, args.service, True, force=args.force)
        if args.command == "disable":
            return cmd_toggle(ctx, args.service, False, force=args.force)
        if args.command == "install":
            return cmd_install(ctx, args.service)
        if args.command == "update":
            return cmd_update(ctx)
        if args.command == "reset":
            return cmd_reset(ctx, args.service)
        if args.command == "doctor":
            return cmd_doctor(ctx)
        if args.command == "resources":
            return cmd_resources(ctx)
        if args.command == "version":
            return cmd_version(ctx)
    except PermissionDenied as exc:
        print(f"eta-admin: {exc}", file=sys.stderr)
        return 2
    except EtaError as exc:
        print(f"eta-admin: {exc}", file=sys.stderr)
        return 1
    except AuthUnavailable as exc:
        print(f"eta-admin: authentication unavailable: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\neta-admin: interrupted", file=sys.stderr)
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
