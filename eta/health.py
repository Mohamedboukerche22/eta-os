"""Readiness and liveness checks.

A systemd unit being "active" is not proof that a service can serve requests.
Each check below verifies the real dependency: SQLite can be opened, MariaDB
answers on its socket, the backend's ``/readyz`` returns 200, and the monitor has
a fresh heartbeat.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .config import Settings, load_settings, service_enabled
from .database import database_health
from .logger import EtaLogger
from .services import REGISTRY, Systemd, enabled_specs, get_spec, required_specs
from .util import atomic_write_json, read_json, utc_iso

STATUS_ORDER = {"ok": 0, "skipped": 1, "degraded": 2, "failed": 3, "unknown": 4}


@dataclass
class HealthResult:
    component: str
    ok: bool
    status: str
    detail: str
    checked_at: str = ""
    checked_epoch: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "checked_at": self.checked_at,
            "checked_epoch": self.checked_epoch,
            "data": self.data,
        }


def _result(
    component: str,
    ok: bool,
    detail: str,
    *,
    status: str | None = None,
    clock: Callable[[], float] = time.time,
    data: Mapping[str, Any] | None = None,
) -> HealthResult:
    now = clock()
    if status is None:
        status = "ok" if ok else "failed"
    return HealthResult(
        component=component,
        ok=ok,
        status=status,
        detail=detail,
        checked_at=utc_iso(now),
        checked_epoch=now,
        data=dict(data or {}),
    )


def http_json(
    url: str,
    *,
    timeout: float = 3.0,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "eta-health"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(65536)
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                payload = {}
            return response.status, payload if isinstance(payload, dict) else {}
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(65536).decode("utf-8", "replace"))
        except (ValueError, OSError):
            payload = {}
        return exc.code, payload if isinstance(payload, dict) else {}
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return 0, {"error": str(exc)}


def check_backend(settings: Settings, *, timeout: float = 3.0) -> HealthResult:
    code, payload = http_json(f"{settings.backend_url}/readyz", timeout=timeout)
    if code == 200 and payload.get("status") == "ready":
        return _result("backend", True, "backend ready", data={"http": code, "database": payload.get("database")})
    if code == 503:
        return _result(
            "backend",
            False,
            f"backend not ready: {payload.get('error') or payload.get('status') or 'database unavailable'}",
            status="degraded",
            data={"http": code, "database": payload.get("database")},
        )
    detail = payload.get("error") or f"HTTP {code or 'unreachable'}"
    return _result("backend", False, f"backend health check failed: {detail}", data={"http": code})


def check_monitor(settings: Settings, *, clock: Callable[[], float] = time.time) -> HealthResult:
    path = settings.run_dir / "health.json"
    state = read_json(path)
    if not isinstance(state, dict):
        return _result("monitor", False, "monitor heartbeat missing", status="degraded")
    updated = state.get("updated_at_epoch")
    try:
        age = clock() - float(updated)
    except (TypeError, ValueError):
        return _result("monitor", False, "monitor heartbeat has no timestamp", status="degraded")
    max_age = float(settings.get("monitor.interval", 15)) * 3 + 5
    if age < 0 or age > max_age:
        return _result(
            "monitor",
            False,
            f"monitor heartbeat stale ({age:.0f}s > {max_age:.0f}s)",
            status="degraded",
            data={"age_seconds": age},
        )
    return _result(
        "monitor",
        True,
        f"heartbeat {age:.0f}s old",
        data={"age_seconds": age, "overall": state.get("overall")},
    )


def check_backup(settings: Settings, *, clock: Callable[[], float] = time.time) -> HealthResult:
    spec = get_spec("backup")
    systemd = Systemd()
    state = systemd.show(spec.unit)
    status = read_json(settings.get("backup.status_file"))
    data: dict[str, Any] = {"timer_active": state.active, "timer_state": state.summary()}
    if not state.active:
        if state.unit_file_state in {"masked", "disabled", ""} and not service_enabled(settings, "backup"):
            return _result("backup", True, "backup disabled", status="skipped")
        return _result("backup", False, "backup timer is not active", status="degraded", data=data)
    if isinstance(status, dict):
        data.update({"last_outcome": status.get("outcome"), "last_end": status.get("last_end")})
        if status.get("outcome") == "failed":
            return _result(
                "backup",
                False,
                f"last backup failed: {status.get('error', 'unknown error')}",
                status="degraded",
                data=data,
            )
        if status.get("outcome") == "running":
            started = status.get("last_start_epoch")
            if isinstance(started, (int, float)) and clock() - float(started) > 6 * 3600:
                return _result("backup", False, "backup appears stuck", status="degraded", data=data)
            return _result("backup", True, "backup running", data=data)
    return _result("backup", True, "backup scheduler active", data=data)


def check_component(
    key: str,
    settings: Settings,
    *,
    systemd: Systemd | None = None,
    clock: Callable[[], float] = time.time,
) -> HealthResult:
    spec = REGISTRY[key]
    sd = systemd or Systemd()
    if key == "database":
        ok, detail = database_health(settings)
        unit = sd.show(spec.unit)
        if not unit.active:
            return _result(key, False, f"database unit {unit.summary()}", data={"unit": unit.unit_file_state})
        return _result(key, ok, detail, status="ok" if ok else "degraded")
    if key == "mariadb":
        ok, detail = database_health(settings) if settings.db_engine == "mariadb" else (True, "mariadb not selected")
        return _result(key, ok, detail, status="ok" if ok else "degraded")
    if key == "backend":
        return check_backend(settings)
    if key == "monitor":
        unit = sd.show(spec.unit)
        if not unit.active:
            return _result(key, False, f"monitor unit {unit.summary()}")
        return check_monitor(settings, clock=clock)
    if key == "backup":
        return check_backup(settings, clock=clock)
    if key == "ssh":
        unit = sd.show(spec.unit)
        if not service_enabled(settings, "ssh"):
            return _result(key, True, "ssh disabled", status="skipped")
        return _result(key, unit.active, f"sshd {unit.summary()}", status="ok" if unit.active else "failed")
    unit = sd.show(spec.unit)
    ok = unit.active
    return _result(key, ok, f"{spec.title}: {unit.summary()}", status="ok" if ok else "failed")


def collect_health(
    settings: Settings,
    keys: Iterable[str] | None = None,
    *,
    systemd: Systemd | None = None,
    clock: Callable[[], float] = time.time,
    required_only: bool = False,
) -> dict[str, HealthResult]:
    sd = systemd or Systemd()
    if keys is None:
        specs = required_specs(settings) if required_only else enabled_specs(settings)
        selected = [spec.key for spec in specs]
    else:
        selected = list(keys)
    results: dict[str, HealthResult] = {}
    for key in selected:
        try:
            results[key] = check_component(key, settings, systemd=sd, clock=clock)
        except Exception as exc:  # never let one broken check hide the others
            results[key] = _result(key, False, f"health check error: {exc}", status="failed")
    return results


def overall_state(settings: Settings, results: Mapping[str, HealthResult]) -> str:
    required = [spec.key for spec in required_specs(settings)]
    failed = [key for key in required if key in results and not results[key].ok]
    any_failed = [key for key, value in results.items() if not value.ok and value.status != "skipped"]
    if failed:
        return "degraded"
    if any_failed:
        return "degraded"
    return "ready"


def health_payload(
    settings: Settings,
    results: Mapping[str, HealthResult],
    *,
    clock: Callable[[], float] = time.time,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    now = clock()
    payload: dict[str, Any] = {
        "schema": "eta.health.v1",
        "updated_at": utc_iso(now),
        "updated_at_epoch": now,
        "overall": overall_state(settings, results),
        "services": {key: value.as_dict() for key, value in results.items()},
        "failed": [key for key, value in results.items() if not value.ok and value.status != "skipped"],
        "required": [spec.key for spec in required_specs(settings)],
    }
    if extra:
        payload.update(extra)
    return payload


def write_health_state(
    settings: Settings,
    results: Mapping[str, HealthResult],
    *,
    clock: Callable[[], float] = time.time,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = health_payload(settings, results, clock=clock, extra=extra)
    path = settings.run_dir / "health.json"
    atomic_write_json(path, payload, mode=0o644)
    return payload


def wait_for(
    settings: Settings,
    keys: Iterable[str],
    *,
    timeout: float = 60.0,
    interval: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    systemd: Systemd | None = None,
) -> dict[str, HealthResult]:
    selected = list(keys)
    deadline = clock() + timeout
    results: dict[str, HealthResult] = {}
    while True:
        results = collect_health(settings, selected, systemd=systemd)
        if all(result.ok for result in results.values()):
            return results
        if clock() >= deadline:
            return results
        sleeper(min(interval, max(0.0, deadline - clock())))


def _human(results: Mapping[str, HealthResult]) -> str:
    lines = []
    for key in sorted(results):
        result = results[key]
        marker = "OK" if result.ok else ("SKIP" if result.status == "skipped" else "FAIL")
        lines.append(f"[{marker}] {key}: {result.detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eta-health", description="Check Eta OS service readiness")
    parser.add_argument("components", nargs="*", help="service keys (default: all enabled)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--wait", type=float, default=0.0, metavar="SECONDS", help="wait for readiness")
    parser.add_argument("--required", action="store_true", help="check required services only")
    parser.add_argument("--quiet", action="store_true", help="exit status only")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"eta-health: configuration error: {exc}", file=sys.stderr)
        return 2
    keys = args.components or None
    if args.components:
        unknown = [key for key in args.components if key not in REGISTRY]
        if unknown:
            print(f"eta-health: unknown service(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
    if args.wait > 0:
        target_keys = keys or [spec.key for spec in (required_specs(settings) if args.required else enabled_specs(settings))]
        results = wait_for(settings, target_keys, timeout=args.wait)
    else:
        results = collect_health(settings, keys, required_only=args.required)
    payload = health_payload(settings, results)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif not args.quiet:
        print(_human(results))
        print(f"overall: {payload['overall']}")
    if keys:
        return 0 if all(r.ok or r.status == "skipped" for r in results.values()) else 1
    return 0 if payload["overall"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
