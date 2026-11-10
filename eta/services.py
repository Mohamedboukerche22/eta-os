"""Service registry and systemd interface.

Eta OS does not guess that a process is healthy because it exists. Every
service has a systemd unit (or timer) plus a health check in :mod:`eta.health`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .config import Settings, service_enabled
from .util import CommandResult, EtaError, Runner, check_safe_unit, run

SYSTEMCTL_ENV = "ETA_SYSTEMCTL"


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    unit: str
    title: str
    kind: str
    priority: int
    essential: bool
    health: str
    description: str
    dependencies: tuple[str, ...] = ()
    installable: bool = False

    @property
    def is_timer(self) -> bool:
        return self.unit.endswith(".timer")


REGISTRY: dict[str, ServiceSpec] = {
    "security": ServiceSpec(
        "security",
        "eta-security.service",
        "Security baseline",
        "security",
        10,
        True,
        "unit",
        "Applies ownership, permission and firewall sanity checks.",
    ),
    "settings": ServiceSpec(
        "settings",
        "eta-settings-apply.service",
        "Runtime settings",
        "system",
        15,
        False,
        "unit",
        "Applies installer-provided hostname and hardware settings.",
    ),
    "database": ServiceSpec(
        "database",
        "eta-database.service",
        "Database",
        "database",
        20,
        True,
        "database",
        "Initializes SQLite or verifies the configured MariaDB instance.",
        ("security",),
    ),
    "mariadb": ServiceSpec(
        "mariadb",
        "mariadb.service",
        "MariaDB (optional)",
        "database",
        22,
        False,
        "mariadb",
        "Optional low-memory MariaDB server.",
        ("database",),
        installable=True,
    ),
    "backend": ServiceSpec(
        "backend",
        "eta-backend.service",
        "Backend API",
        "backend",
        30,
        True,
        "backend",
        "Local HTTP API with liveness and database-aware readiness.",
        ("database",),
    ),
    "monitor": ServiceSpec(
        "monitor",
        "eta-monitor.service",
        "Health monitor",
        "monitor",
        40,
        True,
        "monitor",
        "Checks health, records failures and performs bounded recovery.",
    ),
    "backup": ServiceSpec(
        "backup",
        "eta-backup.timer",
        "Backup scheduler",
        "backup",
        50,
        False,
        "backup",
        "Scheduled, deduplicated restic backups with retention and verification.",
    ),
    "ssh": ServiceSpec(
        "ssh",
        "sshd.service",
        "SSH administration",
        "network",
        60,
        False,
        "unit",
        "Optional SSH access, restricted to configured LAN networks.",
        installable=True,
    ),
}

# Services that must never be disabled without an explicit force flag.
PROTECTED = {"security", "monitor"}
SYSTEM_UNIT_RE = re.compile(r"^(eta-[a-z0-9-]+|mariadb|sshd)\.(service|timer)$")


def get_spec(key: str) -> ServiceSpec:
    try:
        return REGISTRY[key]
    except KeyError as exc:
        raise EtaError(f"unknown service: {key}") from exc


def validate_service_key(key: str) -> str:
    if key not in REGISTRY:
        raise EtaError(
            f"unknown service {key!r}; available: {', '.join(sorted(REGISTRY))}"
        )
    return key


def all_specs() -> list[ServiceSpec]:
    return sorted(REGISTRY.values(), key=lambda spec: (spec.priority, spec.key))


def enabled_specs(settings: Settings) -> list[ServiceSpec]:
    return [spec for spec in all_specs() if service_enabled(settings, spec.key)]


def required_specs(settings: Settings) -> list[ServiceSpec]:
    return [spec for spec in enabled_specs(settings) if spec.essential]


def service_key_for_unit(unit: str) -> str | None:
    for spec in REGISTRY.values():
        if spec.unit == unit:
            return spec.key
    return None


def validate_unit(unit: str) -> str:
    check_safe_unit(unit)
    if not SYSTEM_UNIT_RE.match(unit):
        raise EtaError(f"unit is not managed by Eta OS: {unit!r}")
    return unit


@dataclass
class UnitState:
    unit: str
    active_state: str = "unknown"
    sub_state: str = "unknown"
    result: str = ""
    exit_code: int | None = None
    n_restarts: int = 0
    memory_bytes: int | None = None
    unit_file_state: str = ""
    active_enter_timestamp: str = ""
    next_elapse: str = ""
    main_pid: int | None = None
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.active_state == "active"

    @property
    def enabled(self) -> bool:
        return self.unit_file_state in {
            "enabled",
            "enabled-runtime",
            "static",
            "alias",
            "indirect",
        }

    @property
    def failed(self) -> bool:
        return self.active_state == "failed" or self.result in {"failed", "timeout"} or self.sub_state == "failed"

    def summary(self) -> str:
        if self.active:
            return f"active ({self.sub_state})"
        return f"{self.active_state} ({self.sub_state})"


_PROPERTIES = (
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainStatus",
    "NRestarts",
    "MemoryCurrent",
    "UnitFileState",
    "ActiveEnterTimestamp",
    "NextElapseUSecRealtime",
    "MainPID",
)


def parse_show(text: str) -> UnitState:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    state = UnitState(unit="")
    state.active_state = values.get("ActiveState", "unknown")
    state.sub_state = values.get("SubState", "unknown")
    state.result = values.get("Result", "")
    raw_exit = values.get("ExecMainStatus", "")
    try:
        state.exit_code = int(raw_exit) if raw_exit not in {"", "[not set]"} else None
    except ValueError:
        state.exit_code = None
    try:
        state.n_restarts = int(values.get("NRestarts", "0") or 0)
    except ValueError:
        state.n_restarts = 0
    raw_mem = values.get("MemoryCurrent", "")
    try:
        state.memory_bytes = int(raw_mem) if raw_mem not in {"", "[not set]"} else None
    except ValueError:
        state.memory_bytes = None
    state.unit_file_state = values.get("UnitFileState", "")
    state.active_enter_timestamp = values.get("ActiveEnterTimestamp", "")
    state.next_elapse = values.get("NextElapseUSecRealtime", "")
    try:
        raw_pid = values.get("MainPID", "")
        state.main_pid = int(raw_pid) if raw_pid not in {"", "0", "[not set]"} else None
    except ValueError:
        state.main_pid = None
    state.raw = values
    return state


class Systemd:
    """Thin, testable wrapper around systemctl."""

    def __init__(
        self,
        *,
        systemctl: str | None = None,
        runner: Runner = run,
        timeout: float = 15.0,
    ) -> None:
        self.systemctl = systemctl or os.environ.get(SYSTEMCTL_ENV) or shutil.which("systemctl") or "systemctl"
        self.runner = runner
        self.timeout = timeout

    def _run(self, args: Sequence[str], timeout: float | None = None) -> CommandResult:
        return self.runner(
            [self.systemctl, *args],
            timeout=timeout or self.timeout,
        )

    def show(self, unit: str) -> UnitState:
        # Read-only queries may target systemd targets (local-fs.target).
        check_safe_unit(unit)
        argv = [self.systemctl, "show", unit, "--no-pager"]
        for prop in _PROPERTIES:
            argv.extend(["-p", prop])
        result = self.runner(argv, timeout=self.timeout)
        state = parse_show(result.stdout)
        state.unit = unit
        if not state.raw:
            state.active_state = "unknown"
            state.sub_state = result.stderr.strip()[:200] or "unavailable"
        return state

    def show_many(self, units: Iterable[str]) -> dict[str, UnitState]:
        return {unit: self.show(unit) for unit in units}

    def is_active(self, unit: str) -> bool:
        return self.show(unit).active

    def is_enabled(self, unit: str) -> bool:
        return self.show(unit).enabled

    def start(self, unit: str) -> CommandResult:
        return self._run(["start", validate_unit(unit)])

    def stop(self, unit: str) -> CommandResult:
        return self._run(["stop", validate_unit(unit)])

    def restart(self, unit: str) -> CommandResult:
        return self._run(["restart", validate_unit(unit)], timeout=max(self.timeout, 45.0))

    def enable(self, unit: str) -> CommandResult:
        return self._run(["enable", "--now", validate_unit(unit)], timeout=max(self.timeout, 45.0))

    def disable(self, unit: str) -> CommandResult:
        return self._run(["disable", "--now", validate_unit(unit)], timeout=max(self.timeout, 45.0))

    def daemon_reload(self) -> CommandResult:
        return self._run(["daemon-reload"])

    def reset_failed(self, unit: str) -> CommandResult:
        return self._run(["reset-failed", validate_unit(unit)])

    def overall_state(self) -> str:
        result = self._run(["is-system-running"], timeout=5)
        return (result.stdout or result.stderr).strip().splitlines()[-1] if (result.stdout or result.stderr).strip() else "unknown"

    def failed_units(self) -> list[str]:
        result = self._run(["--failed", "--no-legend", "--plain", "--no-pager"])
        units: list[str] = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts and (parts[0].endswith(".service") or parts[0].endswith(".timer")):
                units.append(parts[0].lstrip("●").strip())
        return units

    def list_timers(self) -> str:
        return self._run(["list-timers", "--all", "--no-pager"]).stdout

    def state_dict(self, units: Iterable[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for unit in units:
            state = self.show(unit)
            out[unit] = {
                "active": state.active_state,
                "sub": state.sub_state,
                "result": state.result,
                "exit_code": state.exit_code,
                "restarts": state.n_restarts,
                "memory_bytes": state.memory_bytes,
                "enabled": state.unit_file_state,
            }
        return out


def services_report(settings: Settings, systemd: Systemd | None = None) -> list[dict[str, Any]]:
    """Combine configured enablement with live systemd state."""
    sd = systemd or Systemd()
    rows: list[dict[str, Any]] = []
    for spec in all_specs():
        state = sd.show(spec.unit)
        rows.append(
            {
                "key": spec.key,
                "unit": spec.unit,
                "title": spec.title,
                "kind": spec.kind,
                "priority": spec.priority,
                "essential": spec.essential,
                "configured": service_enabled(settings, spec.key),
                "enabled": state.enabled,
                "active": state.active,
                "sub_state": state.sub_state,
                "result": state.result,
                "exit_code": state.exit_code,
                "restarts": state.n_restarts,
                "memory_bytes": state.memory_bytes,
                "next_elapse": state.next_elapse,
            }
        )
    return rows


def services_report_json(settings: Settings, systemd: Systemd | None = None) -> str:
    return json.dumps(services_report(settings, systemd), indent=2, sort_keys=True)
