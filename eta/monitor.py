"""Eta OS health monitor.

The monitor is a small long-running process supervised by systemd with a
watchdog. It never restarts services forever: :class:`eta.recovery.RecoveryManager`
enforces a bounded budget and exponential backoff.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import Settings, load_settings
from .health import (
    HealthResult,
    collect_health,
    health_payload,
    overall_state,
    write_health_state,
)
from .logger import EtaLogger
from .recovery import RecoveryManager
from .sdnotify import ready as sd_ready
from .sdnotify import stopping as sd_stopping
from .sdnotify import watchdog as sd_watchdog
from .services import Systemd, enabled_specs, get_spec, service_key_for_unit
from .util import read_json, utc_iso


class Monitor:
    def __init__(
        self,
        settings: Settings,
        *,
        systemd: Systemd | None = None,
        logger: EtaLogger | None = None,
        recovery: RecoveryManager | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        notifier: Callable[[str], bool] | None = None,
        health_collector: Callable[..., Mapping[str, HealthResult]] = collect_health,
    ) -> None:
        self.settings = settings
        self.systemd = systemd or Systemd()
        self.logger = logger or EtaLogger("monitor", settings.log_dir, run_dir=settings.run_dir, clock=clock)
        self.recovery = recovery or RecoveryManager(settings, systemd=self.systemd, logger=self.logger, clock=clock)
        self.clock = clock
        self.sleeper = sleeper
        self.notifier = notifier
        self.health_collector = health_collector
        self._last_failure_signature: dict[str, str] = {}
        self._last_ready = False
        self._last_payload: dict[str, Any] = {}
        self._tick_count = 0

    def _notify(self, state: str) -> None:
        if self.notifier is not None:
            try:
                self.notifier(state)
            except Exception:
                pass

    def _managed_keys(self) -> list[str]:
        keys = []
        for spec in enabled_specs(self.settings):
            if spec.key in {"monitor", "settings"}:
                continue
            keys.append(spec.key)
        return keys

    def tick(self) -> dict[str, Any]:
        self._tick_count += 1
        keys = self._managed_keys()
        results = dict(self.health_collector(keys, systemd=self.systemd, clock=self.clock))
        for key in keys:
            result = results.get(key)
            if result is None:
                continue
            if result.ok:
                was_failing = key in self._last_failure_signature
                self.recovery.note_healthy(key)
                if was_failing:
                    self._last_failure_signature.pop(key, None)
                    self.logger.info(
                        "Service recovered",
                        event="monitor_service_recovered",
                        service=key,
                        detail=result.detail,
                    )
                continue
            self.recovery.note_unhealthy(key)
            signature = f"{result.status}:{result.detail}"
            state = self.systemd.show(get_spec(key).unit)
            first_or_changed = self._last_failure_signature.get(key) != signature
            if first_or_changed:
                self._last_failure_signature[key] = signature
                self.logger.error(
                    f"{key} unhealthy: {result.detail}",
                    event="monitor_service_unhealthy",
                    service=key,
                    status=result.status,
                    unit_state=state.sub_state,
                    exit_code=state.exit_code,
                    recovery_attempt=None,
                    recovery_outcome=None,
                )
            outcome = self.recovery.attempt(key, result.detail, exit_code=state.exit_code)
            if outcome.attempted:
                result.data["recovery"] = outcome.as_dict()
            elif outcome.manual:
                result.data["recovery"] = outcome.as_dict()
            elif outcome.action == "dependency":
                result.data.setdefault("recovery", {})["blocked_by_dependency"] = True
                result.data["recovery"]["detail"] = outcome.detail
        # The monitor's own liveness is this process plus the systemd watchdog.
        results["monitor"] = HealthResult(
            component="monitor",
            ok=True,
            status="ok",
            detail="monitor running",
            checked_at=utc_iso(self.clock()),
            checked_epoch=self.clock(),
            data={"ticks": self._tick_count, "pid": os.getpid()},
        )
        payload = write_health_state(
            self.settings,
            results,
            clock=self.clock,
            extra={
                "monitor_pid": os.getpid(),
                "ticks": self._tick_count,
                "boot_id": _boot_id(),
                "recovery": self.recovery.status(),
            },
        )
        self._last_payload = payload
        if not self._last_ready:
            self._last_ready = True
            self._notify("READY=1")
            ready = payload.get("overall") == "ready"
            self.logger.info(
                "Monitor ready",
                event="monitor_ready",
                overall=payload.get("overall"),
                required=payload.get("required"),
            )
        else:
            self._notify("WATCHDOG=1")
        return payload

    def run_forever(self) -> int:
        stop = threading.Event()

        def handle(signum, _frame):
            self.logger.info("Monitor stopping", event="monitor_stopping", signal=signum)
            stop.set()

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)
        self.logger.info("Monitor started", event="monitor_start", pid=os.getpid())
        sd_ready()
        interval = float(self.settings.get("monitor.interval", 15))
        try:
            while not stop.is_set():
                started = time.monotonic()
                try:
                    self.tick()
                except Exception as exc:
                    self.logger.error(
                        f"Monitor tick failed: {exc}",
                        event="monitor_tick_error",
                        severity="error",
                    )
                elapsed = time.monotonic() - started
                stop.wait(max(1.0, interval - elapsed))
        finally:
            sd_stopping()
            self.logger.info("Monitor stopped", event="monitor_stopped")
        return 0


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eta-monitor", description="Eta OS health monitor")
    parser.add_argument("--once", action="store_true", help="run one tick and print the result")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"eta-monitor: configuration error: {exc}", file=sys.stderr)
        return 2
    monitor = Monitor(settings)
    if args.once:
        payload = monitor.tick()
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"overall: {payload.get('overall')}")
            for key, value in sorted(payload.get("services", {}).items()):
                print(f"{'OK' if value.get('ok') else 'FAIL'} {key}: {value.get('detail')}")
        return 0
    return monitor.run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
