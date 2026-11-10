"""Root helper used by ``eta-admin`` through sudo.

The helper is the only command allowed by the ``eta-admin`` sudoers rule. It
validates every argument against a strict allowlist; it never invokes a shell and
never accepts arbitrary paths, units or nix flags from the caller.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Callable, Sequence

from .backup import BackupManager
from .config import Settings, load_settings
from .logger import EtaLogger, read_log
from .recovery import RecoveryManager
from .services import REGISTRY, Systemd, get_spec, validate_service_key, validate_unit
from .util import CommandResult, Runner, SNAPSHOT_RE, atomic_write_json, file_mode, run, utc_iso

SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
ALLOWED_SYSTEMCTL = {"start", "stop", "restart", "enable", "disable", "reset-failed"}
LOG_COMPONENTS = {
    "boot", "services", "backend", "database", "backup", "security",
    "errors", "recovery", "monitor", "admin", "install",
}
CATALOG_INDEX = Path("/etc/eta/catalog/index.json")
CATALOG_UNITS = Path("/etc/eta/catalog/units")
FLAKE_SOURCE = Path("/etc/eta/source")


class PrivilegedHelper:
    def __init__(
        self,
        settings: Settings,
        *,
        systemd: Systemd | None = None,
        runner: Runner = run,
        logger: EtaLogger | None = None,
    ) -> None:
        self.settings = settings
        self.systemd = systemd or Systemd(runner=runner)
        self.runner = runner
        self.logger = logger or EtaLogger("security", settings.log_dir, run_dir=settings.run_dir)

    def execute(self, argv: Sequence[str]) -> int:
        args = list(argv)
        if not args:
            print("eta-priv: command required", file=sys.stderr)
            return 2
        self.logger.security(
            "Privileged helper invoked",
            event="priv_invoked",
            argv=args,
            uid=os.getuid(),
            euid=os.geteuid(),
        )
        try:
            return self._dispatch(args)
        except Exception as exc:
            self.logger.security(
                f"Privileged helper rejected/failed: {exc}",
                event="priv_error",
                argv=args,
                severity="error",
            )
            print(f"eta-priv: {exc}", file=sys.stderr)
            return 1

    def _dispatch(self, args: list[str]) -> int:
        command = args[0]
        rest = args[1:]
        if command == "systemctl":
            return self._systemctl(rest)
        if command == "daemon-reload":
            return self._run_systemctl(["daemon-reload"])
        if command == "logs":
            return self._logs(rest)
        if command == "journal":
            return self._journal(rest)
        if command == "backup":
            return self._backup(rest)
        if command == "reset-recovery":
            return self._reset_recovery(rest)
        if command == "install-service":
            return self._install_service(rest)
        if command == "update":
            return self._update(rest)
        raise ValueError(f"unknown command: {command}")

    # -- systemctl ----------------------------------------------------------
    def _systemctl(self, args: list[str]) -> int:
        if len(args) != 2 or args[0] not in ALLOWED_SYSTEMCTL:
            raise ValueError(f"usage: eta-priv systemctl {{{'|'.join(sorted(ALLOWED_SYSTEMCTL))}}} UNIT")
        verb, unit = args
        validate_unit(unit)
        systemctl_args = [self.systemd.systemctl, verb]
        if verb in {"enable", "disable"}:
            systemctl_args.append("--now")
        systemctl_args.append(unit)
        result = self.runner(systemctl_args, timeout=60)
        self._finish("systemctl", result)
        return result.returncode

    def _run_systemctl(self, args: list[str]) -> int:
        result = self.runner([self.systemd.systemctl, *args], timeout=60)
        self._finish("systemctl", result)
        return result.returncode

    def _finish(self, action: str, result: CommandResult) -> None:
        self.logger.security(
            f"Privileged {action} finished",
            event="priv_result",
            exit_code=result.returncode,
            output=(result.output or "")[:500],
        )
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)

    # -- logs ---------------------------------------------------------------
    def _logs(self, args: list[str]) -> int:
        if not args or args[0] not in LOG_COMPONENTS:
            raise ValueError(f"usage: eta-priv logs COMPONENT [LINES]")
        component = args[0]
        lines = 100
        if len(args) > 1:
            if not args[1].isdigit() or int(args[1]) > 5000:
                raise ValueError("lines must be an integer <= 5000")
            lines = int(args[1])
        records = read_log(component, lines=lines, log_dir=self.settings.log_dir)
        for record in records:
            print(json.dumps(record, ensure_ascii=False))
        return 0

    def _journal(self, args: list[str]) -> int:
        if len(args) != 2 or not args[1].isdigit():
            raise ValueError("usage: eta-priv journal UNIT LINES")
        unit = validate_unit(args[0])
        lines = min(int(args[1]), 2000)
        journalctl = shutil.which("journalctl")
        if not journalctl:
            raise ValueError("journalctl not found")
        result = self.runner(
            [journalctl, "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"],
            timeout=30,
        )
        self._finish("journal", result)
        return result.returncode

    # -- backup -------------------------------------------------------------
    def _backup(self, args: list[str]) -> int:
        if not args:
            raise ValueError("usage: eta-priv backup {status|run|list|restore}")
        action = args[0]
        manager = BackupManager(self.settings, logger=EtaLogger("backup", self.settings.log_dir))
        if action == "status":
            print(json.dumps(manager.status(), indent=2, sort_keys=True, default=str))
            return 0
        if action == "run":
            payload = manager.run(reason="admin")
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
            return 0 if payload.get("outcome") == "success" else 1
        if action == "list":
            print(json.dumps(manager.list_snapshots(), indent=2, sort_keys=True))
            return 0
        if action == "restore":
            parser = argparse.ArgumentParser(prog="eta-priv backup restore")
            parser.add_argument("--snapshot", default="latest")
            parser.add_argument("--target")
            parser.add_argument("--dry-run", action="store_true")
            parser.add_argument("--overwrite", action="store_true")
            parser.add_argument("--confirm", action="store_true")
            parsed = parser.parse_args(args[1:])
            if not parsed.confirm:
                raise ValueError("restore requires --confirm")
            if parsed.snapshot != "latest" and not SNAPSHOT_RE.match(parsed.snapshot):
                raise ValueError("invalid snapshot id")
            payload = manager.restore(
                parsed.snapshot,
                parsed.target,
                dry_run=parsed.dry_run,
                overwrite=parsed.overwrite,
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        raise ValueError(f"unknown backup action: {action}")

    # -- recovery -----------------------------------------------------------
    def _reset_recovery(self, args: list[str]) -> int:
        if len(args) != 1:
            raise ValueError("usage: eta-priv reset-recovery SERVICE")
        key = validate_service_key(args[0])
        manager = RecoveryManager(self.settings, systemd=self.systemd, logger=self.logger)
        manager.reset(key)
        print(f"recovery state reset for {key}")
        return 0

    # -- offline install / update ------------------------------------------
    def _install_service(self, args: list[str]) -> int:
        if len(args) != 1 or not SERVICE_NAME_RE.match(args[0]):
            raise ValueError("usage: eta-priv install-service NAME")
        name = args[0]
        if name in REGISTRY and REGISTRY[name].installable:
            spec = get_spec(name)
            self.logger.security("Installing bundled service", event="priv_install_service", service=name, unit=spec.unit)
            result = self.runner([self.systemd.systemctl, "enable", "--now", spec.unit], timeout=60)
            self._finish("install-service", result)
            return result.returncode
        catalog = _load_catalog()
        if name not in catalog:
            raise ValueError(f"{name!r} is not in the offline service catalog")
        entry = catalog[name]
        if not isinstance(entry, dict):
            raise ValueError("invalid catalog entry")
        unit_name = str(entry.get("unit", ""))
        validate_unit(unit_name)
        source = CATALOG_UNITS / f"{name}.service"
        if not source.is_file():
            raise ValueError(f"catalog unit file missing: {source}")
        stat = source.stat()
        if stat.st_uid != 0 or stat.st_mode & 0o022:
            raise ValueError("catalog unit must be root-owned and not group/world writable")
        target = Path("/etc/systemd/system") / unit_name
        shutil.copyfile(source, target)
        os.chmod(target, 0o644)
        self.logger.security(
            "Installed catalog service",
            event="priv_install_service",
            service=name,
            unit=unit_name,
        )
        reload_result = self.runner([self.systemd.systemctl, "daemon-reload"], timeout=30)
        if not reload_result.ok:
            self._finish("daemon-reload", reload_result)
            return reload_result.returncode
        result = self.runner([self.systemd.systemctl, "enable", "--now", unit_name], timeout=60)
        self._finish("install-service", result)
        return result.returncode

    def _update(self, args: list[str]) -> int:
        if args:
            raise ValueError("eta-priv update accepts no arguments")
        if not (FLAKE_SOURCE / "flake.nix").is_file():
            raise ValueError(f"offline update source missing: {FLAKE_SOURCE}")
        nixos_rebuild = shutil.which("nixos-rebuild")
        if not nixos_rebuild:
            raise ValueError("nixos-rebuild not found")
        # --offline plus an empty substituter list guarantees no network fetch.
        argv = [
            nixos_rebuild,
            "switch",
            "--flake",
            f"path:{FLAKE_SOURCE}#eta",
            "--offline",
            "--option",
            "substituters",
            "",
        ]
        self.logger.security(
            "Offline system update started",
            event="priv_update_start",
            source=str(FLAKE_SOURCE),
        )
        result = self.runner(argv, timeout=3600)
        self.logger.security(
            "Offline system update finished",
            event="priv_update_result",
            exit_code=result.returncode,
            output=(result.output or "")[:1000],
        )
        self._finish("update", result)
        return result.returncode


def _load_catalog() -> dict:
    try:
        import json as _json

        with open(CATALOG_INDEX, "r", encoding="utf-8") as handle:
            data = _json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if os.geteuid() != 0:
        print("eta-priv: must be run as root (through sudo)", file=sys.stderr)
        return 2
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"eta-priv: configuration error: {exc}", file=sys.stderr)
        return 2
    return PrivilegedHelper(settings).execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
