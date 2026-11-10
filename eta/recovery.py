"""Bounded automatic recovery.

Rules:

* At most ``monitor.max_attempts`` restarts inside ``monitor.attempt_window``.
* Exponential backoff between attempts, capped at ``monitor.backoff_max``.
* After exhaustion the service is parked; the monitor keeps reporting it but
  stops restarting it until an administrator intervenes or the attempt window
  expires.
* Corrupt SQLite databases are never silently restored. That needs a human.
* A backend is not restarted while its database is unhealthy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .database import check_sqlite, database_health
from .logger import EtaLogger
from .services import REGISTRY, Systemd, get_spec
from .util import atomic_write_json, read_json, utc_iso


@dataclass
class RecoveryOutcome:
    service: str
    attempted: bool
    ok: bool
    action: str
    detail: str
    attempt_count: int = 0
    next_attempt_epoch: float = 0.0
    manual: bool = False
    exit_code: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "attempted": self.attempted,
            "ok": self.ok,
            "action": self.action,
            "detail": self.detail,
            "attempt_count": self.attempt_count,
            "next_attempt_epoch": self.next_attempt_epoch,
            "manual": self.manual,
            "exit_code": self.exit_code,
            "data": self.data,
        }


class RecoveryManager:
    def __init__(
        self,
        settings: Settings,
        *,
        systemd: Systemd | None = None,
        logger: EtaLogger | None = None,
        state_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
        health_waiter: Callable[..., dict] | None = None,
    ) -> None:
        self.settings = settings
        self.systemd = systemd or Systemd()
        self.logger = logger or EtaLogger("recovery", settings.log_dir, run_dir=settings.run_dir)
        self.state_path = Path(state_path or (settings.state_dir / "recovery.json"))
        self.clock = clock
        self.health_waiter = health_waiter
        self._state = self._load()
        self._noticed: dict[str, str] = {}

    # -- state --------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        data = read_json(self.state_path, {})
        if not isinstance(data, dict):
            data = {}
        services = data.get("services")
        if not isinstance(services, dict):
            data["services"] = {}
        data.setdefault("schema", "eta.recovery.v1")
        return data

    def _save(self) -> None:
        try:
            atomic_write_json(self.state_path, self._state, mode=0o600)
        except OSError as exc:
            self.logger.error(f"cannot persist recovery state: {exc}", event="recovery_state_error")

    def _service_state(self, service: str) -> dict[str, Any]:
        services = self._state.setdefault("services", {})
        state = services.setdefault(service, {})
        if not isinstance(state, dict):
            state = {}
            services[service] = state
        state.setdefault("attempts", [])
        state.setdefault("next_allowed", 0.0)
        state.setdefault("healthy_since", None)
        state.setdefault("last_outcome", None)
        return state

    def _prune(self, state: dict[str, Any], now: float) -> None:
        window = float(self.settings.get("monitor.attempt_window", 1800))
        state["attempts"] = [
            float(ts) for ts in state.get("attempts", []) if isinstance(ts, (int, float)) and now - float(ts) <= window
        ]

    def attempts(self, service: str) -> int:
        state = self._service_state(service)
        self._prune(state, self.clock())
        return len(state["attempts"])

    def next_allowed(self, service: str) -> float:
        return float(self._service_state(service).get("next_allowed", 0.0) or 0.0)

    def reset(self, service: str) -> None:
        state = self._service_state(service)
        state["attempts"] = []
        state["next_allowed"] = 0.0
        state["healthy_since"] = None
        state["last_outcome"] = "reset"
        self._save()
        self.logger.info("Recovery state reset", event="recovery_reset", service=service)

    def note_healthy(self, service: str) -> None:
        state = self._service_state(service)
        now = self.clock()
        stable = float(self.settings.get("monitor.stable_reset", 600))
        since = state.get("healthy_since")
        if not isinstance(since, (int, float)):
            state["healthy_since"] = now
            self._save()
            return
        if now - float(since) >= stable:
            if state.get("attempts"):
                self.logger.info(
                    "Service stable; recovery budget reset",
                    event="recovery_budget_reset",
                    service=service,
                    stable_seconds=round(now - float(since), 1),
                )
            state["attempts"] = []
            state["next_allowed"] = 0.0
            state["healthy_since"] = now
            self._save()

    def note_unhealthy(self, service: str) -> None:
        state = self._service_state(service)
        state["healthy_since"] = None

    # -- recovery -----------------------------------------------------------
    def can_attempt(self, service: str) -> bool:
        state = self._service_state(service)
        now = self.clock()
        self._prune(state, now)
        maximum = int(self.settings.get("monitor.max_attempts", 3))
        return len(state["attempts"]) < maximum and now >= float(state.get("next_allowed", 0) or 0)

    def _wait_health(self, service: str, timeout: float = 35.0) -> tuple[bool, str]:
        if self.health_waiter is not None:
            results = self.health_waiter(self.settings, [service], timeout=timeout)
        else:
            from .health import wait_for

            results = wait_for(self.settings, [service], timeout=timeout)
        result = results.get(service)
        if result is None:
            return False, "health check unavailable"
        return result.ok, result.detail

    def attempt(
        self,
        service: str,
        reason: str,
        *,
        exit_code: int | None = None,
    ) -> RecoveryOutcome:
        spec = get_spec(service)
        state = self._service_state(service)
        now = self.clock()
        self._prune(state, now)
        maximum = int(self.settings.get("monitor.max_attempts", 3))
        if len(state["attempts"]) >= maximum:
            self._notice_once(service, "exhausted", now)
            return RecoveryOutcome(
                service=service,
                attempted=False,
                ok=False,
                action="none",
                detail=f"recovery exhausted ({len(state['attempts'])}/{maximum} attempts in window); manual action required",
                attempt_count=len(state["attempts"]),
                next_attempt_epoch=float(state.get("next_allowed", 0) or 0),
                manual=True,
            )
        if now < float(state.get("next_allowed", 0) or 0):
            return RecoveryOutcome(
                service=service,
                attempted=False,
                ok=False,
                action="backoff",
                detail=f"backoff active until {utc_iso(float(state['next_allowed']))}",
                attempt_count=len(state["attempts"]),
                next_attempt_epoch=float(state["next_allowed"]),
            )

        # Dependency guard: do not restart a backend whose database is down.
        if service == "backend":
            db_ok, db_detail = database_health(self.settings)
            if not db_ok:
                return RecoveryOutcome(
                    service=service,
                    attempted=False,
                    ok=False,
                    action="dependency",
                    detail=f"backend left running; database unavailable: {db_detail}",
                    attempt_count=len(state["attempts"]),
                )
        if service == "database" and self.settings.db_engine == "sqlite":
            ok, detail = check_sqlite(self.settings.sqlite_path, quick=True)
            if not ok and "missing" not in detail:
                self.logger.critical(
                    f"SQLite integrity problem; refusing automatic recovery: {detail}",
                    event="recovery_manual",
                    service=service,
                    exit_code=exit_code,
                    recovery_attempt=len(state["attempts"]) + 1,
                    recovery_outcome="manual",
                )
                return RecoveryOutcome(
                    service=service,
                    attempted=False,
                    ok=False,
                    action="manual",
                    detail=f"SQLite integrity problem: {detail}",
                    attempt_count=len(state["attempts"]),
                    manual=True,
                )

        state["attempts"].append(now)
        attempt_number = len(state["attempts"])
        base = float(self.settings.get("monitor.backoff_base", 5))
        maximum_backoff = float(self.settings.get("monitor.backoff_max", 120))
        delay = min(base * (2 ** (attempt_number - 1)), maximum_backoff)
        state["next_allowed"] = now + delay
        self._save()
        self.logger.warning(
            f"Recovery attempt {attempt_number}/{maximum} for {service}: {reason}",
            event="recovery_attempt",
            service=service,
            reason=reason,
            attempt=attempt_number,
            max_attempts=maximum,
            backoff_seconds=delay,
            exit_code=exit_code,
        )

        action, result = self._perform(service)
        ok = result.ok
        detail = (result.output or f"exit code {result.returncode}")[:500]
        if ok:
            healthy, health_detail = self._wait_health(service)
            ok = healthy
            detail = health_detail
        state["last_outcome"] = "recovered" if ok else "failed"
        state["last_attempt"] = now
        self._save()
        outcome = RecoveryOutcome(
            service=service,
            attempted=True,
            ok=ok,
            action=action,
            detail=detail,
            attempt_count=attempt_number,
            next_attempt_epoch=float(state["next_allowed"]),
            exit_code=result.returncode,
        )
        log = self.logger.info if ok else self.logger.error
        log(
            f"Recovery {'succeeded' if ok else 'failed'} for {service} ({action})",
            event="recovery_result",
            service=service,
            action=action,
            recovery_attempt=attempt_number,
            recovery_outcome="recovered" if ok else "failed",
            exit_code=result.returncode,
            detail=detail,
        )
        return outcome

    def _perform(self, service: str):
        spec = get_spec(service)
        if service == "monitor":
            return "systemd", _FakeResult(False, "monitor is supervised by systemd; self-restart refused")
        if service == "backup":
            return "timer-start", self.systemd.start(spec.unit)
        if service == "database" and self.settings.db_engine == "mariadb":
            maria = self.systemd.restart(REGISTRY["mariadb"].unit)
            if not maria.ok:
                return "mariadb-restart", maria
            return "database-restart", self.systemd.restart(spec.unit)
        return f"restart:{spec.unit}", self.systemd.restart(spec.unit)

    def _notice_once(self, service: str, outcome: str, now: float) -> None:
        key = f"{service}:{outcome}"
        if self._noticed.get(service) == key:
            return
        self._noticed[service] = key
        self.logger.critical(
            f"Service {service} exhausted its bounded recovery budget",
            event="recovery_exhausted",
            service=service,
            recovery_attempt=self.attempts(service),
            recovery_outcome="exhausted",
            manual_action="eta-admin restart %s (after investigation)" % service,
        )

    def status(self) -> dict[str, Any]:
        now = self.clock()
        out: dict[str, Any] = {"schema": "eta.recovery.v1", "updated_at": utc_iso(now), "services": {}}
        for service, state in self._state.get("services", {}).items():
            self._prune(state, now)
            out["services"][service] = {
                "attempts": len(state.get("attempts", [])),
                "next_allowed_epoch": state.get("next_allowed", 0),
                "next_allowed": utc_iso(float(state.get("next_allowed", 0) or 0)) if state.get("next_allowed") else None,
                "healthy_since": state.get("healthy_since"),
                "last_outcome": state.get("last_outcome"),
            }
        return out


class _FakeResult:
    def __init__(self, ok: bool, output: str) -> None:
        self.returncode = 0 if ok else 1
        self.stdout = output if ok else ""
        self.stderr = "" if ok else output

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()
