"""Authenticated rescue/maintenance environment.

This session is intentionally not a second init system. It runs while systemd and
all services keep running. Ctrl+C only pauses the boot display; this module never
signals PID 1.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, TextIO

from . import BANNER, IDENTITY_LINE, __version__
from .auth import AuthUnavailable, authenticator_from_settings, require_root
from .backup import BackupManager
from .config import Settings, load_settings
from .logger import EtaLogger, read_log, tail_file
from .services import REGISTRY, Systemd, get_spec, validate_service_key
from .util import EtaError, file_mode, format_duration, human_bytes, read_json, run, uptime_seconds

EDIT_ROOTS = ("/etc/eta", "/etc/nixos")
LOG_COMPONENTS = ("boot", "services", "backend", "database", "backup", "security", "errors", "recovery", "monitor", "admin", "install")

HELP = """
Maintenance commands
  status                 Short system status
  services               List services and health
  restart SERVICE        Restart one Eta OS service
  logs [COMPONENT] [N]   Show recent persistent log records
  diagnostics            Memory, disk, kernel, failed units, hardware
  network                Interfaces, routes and link state
  edit PATH              Edit a file under /etc/eta or /etc/nixos
  scripts                List approved maintenance scripts
  run SCRIPT             Run a root-owned script from /etc/eta/maintenance.d
  backup status          Backup status
  backup run             Run a backup now (low priority)
  backup restore         Restore a snapshot to the staging area
  shell                  Open a root shell (logged)
  reboot | poweroff      Restart or power off (confirmation required)
  exit                   Return to the boot display
  help                   Show this help
"""


class MaintenanceSession:
    def __init__(
        self,
        settings: Settings,
        *,
        logger: EtaLogger | None = None,
        systemd: Systemd | None = None,
        terminal: TextIO | None = None,
        input_func: Callable[[str], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.logger = logger or EtaLogger("recovery", settings.log_dir, run_dir=settings.run_dir, clock=clock)
        self.systemd = systemd or Systemd()
        self.stream = terminal or sys.stdout
        self.input_func = input_func or input
        self.clock = clock
        self._running = True

    def _write(self, text: str = "") -> None:
        try:
            self.stream.write(text + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def _ask(self, prompt: str) -> str:
        try:
            return self.input_func(prompt)
        except EOFError:
            return "exit"

    def _confirm(self, prompt: str) -> bool:
        answer = self._ask(prompt).strip().lower()
        return answer in {"yes", "y"}

    def run(self) -> int:
        self._write(BANNER)
        self._write(f"MAINTENANCE MODE - Eta OS {__version__}")
        self._write("Services are still running. Type 'help' for commands.\n")
        self.logger.security("Maintenance session opened", event="maintenance_open")
        while self._running:
            try:
                line = self._ask("eta-maintenance> ").strip()
            except KeyboardInterrupt:
                self._write("")
                continue
            if not line:
                continue
            try:
                self._dispatch(line)
            except EtaError as exc:
                self._write(f"error: {exc}")
            except Exception as exc:
                self._write(f"unexpected error: {type(exc).__name__}: {exc}")
                self.logger.error(
                    f"Maintenance command failed: {line}: {exc}",
                    event="maintenance_command_error",
                )
        self.logger.security("Maintenance session closed", event="maintenance_close")
        return 0

    def _dispatch(self, line: str) -> None:
        parts = line.split()
        command = parts[0].lower()
        args = parts[1:]
        if command in {"exit", "quit", "resume"}:
            self._write("Leaving maintenance mode.")
            self._running = False
        elif command == "help":
            self._write(HELP)
        elif command == "status":
            self.cmd_status()
        elif command == "services":
            self.cmd_services()
        elif command == "restart":
            self.cmd_restart(args)
        elif command == "logs":
            self.cmd_logs(args)
        elif command == "diagnostics":
            self.cmd_diagnostics()
        elif command == "network":
            self.cmd_network()
        elif command == "edit":
            self.cmd_edit(args)
        elif command == "scripts":
            self.cmd_scripts()
        elif command == "run":
            self.cmd_run_script(args)
        elif command == "backup":
            self.cmd_backup(args)
        elif command == "shell":
            self.cmd_shell()
        elif command in {"reboot", "poweroff"}:
            self.cmd_power(command)
        else:
            self._write(f"unknown command: {command} (try 'help')")

    # -- read-only ----------------------------------------------------------
    def cmd_status(self) -> None:
        self._write(f"hostname:  {os.uname().nodename}")
        self._write(f"kernel:    {os.uname().sysname} {os.uname().release}")
        self._write(f"uptime:    {format_duration(uptime_seconds(self.clock))}")
        self._write(f"eta:       {__version__} (NixOS 26.05)")
        self._write(f"systemd:   {self.systemd.overall_state()}")
        failed = self.systemd.failed_units()
        self._write(f"failed:    {', '.join(failed) if failed else 'none'}")
        mem = _meminfo()
        if mem:
            self._write(f"memory:    {human_bytes(mem.get('MemAvailable', 0))} available / {human_bytes(mem.get('MemTotal', 0))} total")
        try:
            usage = shutil.disk_usage("/")
            self._write(f"disk:      {human_bytes(usage.free)} free / {human_bytes(usage.total)} total")
        except OSError:
            pass

    def cmd_services(self) -> None:
        for key in sorted(REGISTRY):
            spec = REGISTRY[key]
            state = self.systemd.show(spec.unit)
            marker = "active" if state.active else state.active_state
            self._write(f"{key:12} {marker:12} enabled={state.unit_file_state or 'no':10} {spec.unit}")

    def cmd_logs(self, args: list[str]) -> None:
        component = args[0] if args else "errors"
        lines = int(args[1]) if len(args) > 1 and args[1].isdigit() else 40
        if component not in LOG_COMPONENTS:
            raise EtaError(f"unknown log component: {component}")
        records = read_log(component, lines=lines, log_dir=self.settings.log_dir)
        if not records:
            self._write(f"no records in {component}.log")
            return
        for record in records:
            self._write(
                f"{record.get('ts')} {str(record.get('severity', '')).upper():8} "
                f"{record.get('component')}: {record.get('message')}"
            )

    def cmd_diagnostics(self) -> None:
        self._write(f"kernel:  {os.uname().sysname} {os.uname().release} {os.uname().machine}")
        self._write(f"uptime:  {format_duration(uptime_seconds(self.clock))}")
        self._write(f"loadavg: {' '.join(Path('/proc/loadavg').read_text().split()[:3]) if Path('/proc/loadavg').exists() else 'n/a'}")
        mem = _meminfo()
        for key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            if key in mem:
                self._write(f"{key}: {human_bytes(mem[key])}")
        try:
            usage = shutil.disk_usage("/")
            self._write(f"disk:    {human_bytes(usage.used)} used, {human_bytes(usage.free)} free")
        except OSError:
            pass
        failed = self.systemd.failed_units()
        self._write(f"failed units: {', '.join(failed) if failed else 'none'}")
        for binary, args in (
            ("lscpu", ["lscpu"]),
            ("lsblk", ["lsblk", "-o", "NAME,SIZE,TYPE,MOUNTPOINT"]),
            ("smartctl", ["smartctl", "--scan"]),
        ):
            path = shutil.which(binary)
            if not path:
                continue
            result = run([path, *args[1:]], timeout=15)
            if result.stdout:
                self._write(f"\n$ {binary}\n{result.stdout.strip()[:4000]}")
        dmesg = shutil.which("dmesg")
        if dmesg:
            result = run([dmesg, "--level=err,warn"], timeout=10)
            if result.stdout:
                self._write("\nrecent kernel warnings/errors:\n" + "\n".join(result.stdout.splitlines()[-20:]))

    def cmd_network(self) -> None:
        for binary, args in (
            ("ip", ["ip", "-brief", "address"]),
            ("ip", ["ip", "route"]),
            ("ss", ["ss", "-lntup"]),
        ):
            path = shutil.which(binary)
            if not path:
                continue
            result = run([path, *args[1:]], timeout=10)
            self._write(f"$ {' '.join(args)}\n{result.stdout.strip()}\n")

    # -- mutations ----------------------------------------------------------
    def cmd_restart(self, args: list[str]) -> None:
        if not args:
            raise EtaError("usage: restart SERVICE")
        key = validate_service_key(args[0])
        spec = get_spec(key)
        if not self._confirm(f"Restart {spec.title} ({spec.unit})? [y/N] "):
            return
        self.logger.security("Maintenance restart requested", event="maintenance_restart", service=key)
        result = self.systemd.restart(spec.unit)
        self._write(f"restart exit code: {result.returncode}")
        if result.output:
            self._write(result.output[:2000])

    def cmd_edit(self, args: list[str]) -> None:
        if not args:
            raise EtaError("usage: edit /etc/eta/<file>")
        target = Path(args[0]).resolve()
        allowed = False
        for root in EDIT_ROOTS:
            root_path = Path(root).resolve()
            if target == root_path or root_path in target.parents:
                allowed = True
                break
        if not allowed:
            raise EtaError(f"editing is restricted to {', '.join(EDIT_ROOTS)}")
        if not target.exists():
            raise EtaError(f"file does not exist: {target}")
        if target.is_dir():
            raise EtaError("cannot edit a directory")
        backup = target.with_name(target.name + f".bak.{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(target, backup)
        os.chmod(backup, 0o600)
        editor = os.environ.get("EDITOR") or shutil.which("nano") or shutil.which("vi") or "/bin/ed"
        self.logger.security("Maintenance file edit", event="maintenance_edit", path=str(target))
        result = subprocess.run([editor, str(target)])
        if result.returncode != 0:
            self._write(f"editor exited with {result.returncode}; original backed up at {backup}")
        if target.suffix == ".json":
            data = read_json(target)
            if data is None and target.stat().st_size > 0:
                self._write("warning: file is not valid JSON; restore the backup if needed", )
        self._write(f"backup: {backup}")

    def cmd_scripts(self) -> None:
        directory = Path(str(self.settings.get("maintenance.script_dir", "/etc/eta/maintenance.d")))
        scripts = self._safe_scripts(directory)
        if not scripts:
            self._write(f"no approved scripts in {directory}")
            return
        for path, problem in scripts:
            if problem:
                self._write(f"BLOCKED {path}: {problem}")
            else:
                self._write(f"ok      {path}")

    def _safe_scripts(self, directory: Path):
        results: list[tuple[Path, str | None]] = []
        try:
            paths = sorted(directory.glob("*.sh"))
        except OSError:
            return results
        for path in paths:
            problem: str | None = None
            try:
                stat = path.stat()
                if stat.st_uid != 0:
                    problem = "not owned by root"
                elif stat.st_mode & 0o022:
                    problem = "group/world writable"
                elif not os.access(path, os.R_OK):
                    problem = "not readable"
            except OSError as exc:
                problem = str(exc)
            results.append((path, problem))
        return results

    def cmd_run_script(self, args: list[str]) -> None:
        if not self.settings.get("maintenance.allow_custom_scripts", True):
            raise EtaError("custom maintenance scripts are disabled")
        if not args:
            raise EtaError("usage: run /etc/eta/maintenance.d/script.sh")
        target = Path(args[0]).resolve()
        directory = Path(str(self.settings.get("maintenance.script_dir", "/etc/eta/maintenance.d"))).resolve()
        if directory not in target.parents:
            raise EtaError("script must live in the maintenance script directory")
        if not target.is_file():
            raise EtaError(f"script not found: {target}")
        stat = target.stat()
        if stat.st_uid != 0:
            raise EtaError("refusing to run a script not owned by root")
        if stat.st_mode & 0o022:
            raise EtaError("refusing to run a group/world-writable script")
        if not self._confirm(f"Run {target} as root? [y/N] "):
            return
        self.logger.security("Maintenance script started", event="maintenance_script", path=str(target))
        result = subprocess.run(["bash", str(target)], env={**os.environ, "ETA_MAINTENANCE": "1"})
        self.logger.security(
            "Maintenance script finished",
            event="maintenance_script_done",
            path=str(target),
            exit_code=result.returncode,
        )
        self._write(f"script exit code: {result.returncode}")

    def cmd_backup(self, args: list[str]) -> None:
        manager = BackupManager(self.settings)
        action = args[0] if args else "status"
        if action == "status":
            payload = manager.status()
            self._write(json.dumps(payload, indent=2, sort_keys=True, default=str))
            return
        if action == "run":
            if not self._confirm("Run a backup now? [y/N] "):
                return
            payload = manager.run(reason="maintenance")
            self._write(json.dumps(payload, indent=2, sort_keys=True, default=str))
            return
        if action == "restore":
            snapshots = manager.list_snapshots(limit=10)
            for row in snapshots:
                self._write(f"{row.get('short_id') or row.get('id')}  {row.get('time')}")
            snapshot = self._ask("snapshot id (or latest): ").strip() or "latest"
            target = self._ask("staging target (blank for automatic): ").strip() or None
            if not self._confirm(f"Restore {snapshot} to {target or manager.restore_root}? [y/N] "):
                return
            payload = manager.restore(snapshot, target)
            self._write(json.dumps(payload, indent=2, sort_keys=True))
            return
        raise EtaError("usage: backup [status|run|restore]")

    def cmd_shell(self) -> None:
        if not self._confirm("Open a root shell? Type 'exit' to return. [y/N] "):
            return
        shell = os.environ.get("SHELL") or "/bin/bash"
        self.logger.security("Maintenance root shell opened", event="maintenance_shell")
        subprocess.call([shell])
        self.logger.security("Maintenance root shell closed", event="maintenance_shell_close")

    def cmd_power(self, command: str) -> None:
        if not self._confirm(f"{command} now? [y/N] "):
            return
        self.logger.security(f"Maintenance requested {command}", event=f"maintenance_{command}")
        result = run(["systemctl", command], timeout=30)
        if result.returncode != 0:
            self._write(result.output)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eta-maintenance", description="Eta OS maintenance environment")
    parser.add_argument("--require-auth", action="store_true", default=True)
    parser.add_argument("--no-auth", action="store_true", help="only valid for the boot display path")
    args = parser.parse_args(argv)
    try:
        require_root()
    except Exception as exc:
        print(f"eta-maintenance: {exc}", file=sys.stderr)
        return 2
    settings = load_settings()
    logger = EtaLogger("recovery", settings.log_dir, run_dir=settings.run_dir)
    if args.require_auth and not args.no_auth:
        auth = authenticator_from_settings(settings, logger)
        try:
            if not auth.authenticate_interactive(attempts=int(settings.get("maintenance.max_auth_attempts", 3))):
                print("Access denied.", file=sys.stderr)
                return 1
        except AuthUnavailable as exc:
            print(f"Authentication unavailable: {exc}", file=sys.stderr)
            return 1
    session = MaintenanceSession(settings, logger=logger)
    return session.run()


if __name__ == "__main__":
    raise SystemExit(main())
