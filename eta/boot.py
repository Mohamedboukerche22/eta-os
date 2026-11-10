"""Eta OS boot experience.

This is a real systemd oneshot service that runs on the console. It reports the
state of actual units and readiness checks, not the mere existence of PIDs.

Ctrl+C is delivered to this process only. It never sends a signal to PID 1 and
never stops services. It pauses this display, asks for the master password, and
opens the maintenance environment.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import sys
import termios
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TextIO

from . import BANNER, IDENTITY_LINE, PRODUCT, UPSTREAM, __version__
from .auth import AuthUnavailable, authenticator_from_settings
from .config import Settings, load_settings, service_enabled
from .health import HealthResult, collect_health, overall_state, write_health_state
from .logger import EtaLogger
from .services import Systemd, enabled_specs, get_spec
from .util import atomic_write_json, format_duration, uptime_seconds

GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

STATUS_STYLES = {
    "ok": (GREEN, "OK"),
    "warn": (YELLOW, "WARN"),
    "fail": (RED, "FAIL"),
    "skip": (DIM, "SKIP"),
    "unknown": (YELLOW, "????"),
}


class Terminal:
    def __init__(
        self,
        streams: Iterable[TextIO] | None = None,
        *,
        color: bool | None = None,
    ) -> None:
        self.streams = list(streams or [sys.stdout])
        if color is None:
            color = (
                any(getattr(stream, "isatty", lambda: False)() for stream in self.streams)
                and "NO_COLOR" not in os.environ
            )
        self.color = bool(color)

    def _style(self, text: str, code: str | None) -> str:
        if not self.color or not code:
            return text
        return f"{code}{text}{RESET}"

    def write(self, text: str = "") -> None:
        for stream in self.streams:
            try:
                stream.write(text)
                stream.flush()
            except (OSError, ValueError):
                continue

    def line(self, text: str = "", code: str | None = None) -> None:
        self.write(self._style(text, code) + "\n")

    def status(self, status: str, title: str, duration: float | None = None, detail: str = "") -> None:
        code, label = STATUS_STYLES.get(status, STATUS_STYLES["unknown"])
        prefix = self._style(f"[ {label} ]", code)
        suffix = f" ({format_duration(duration)})" if duration is not None else ""
        extra = f" - {detail}" if detail and status in {"warn", "fail"} else ""
        self.write(f"{prefix} {title}{suffix}{extra}\n")

    def pending(self, title: str) -> None:
        self.write(f"{self._style('[ .. ]', CYAN)} {title}...\n")

    def blank(self) -> None:
        self.write("\n")


@dataclass
class PhaseSpec:
    key: str
    title: str
    required: bool
    timeout: float = 45.0
    service: str | None = None
    enabled: Callable[[Settings], bool] = lambda _s: True


@dataclass
class PhaseResult:
    key: str
    title: str
    status: str
    detail: str
    duration: float
    required: bool
    exit_code: int | None = None
    recovery: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "skip"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "duration_seconds": round(self.duration, 3),
            "required": self.required,
            "exit_code": self.exit_code,
            "recovery": self.recovery,
        }


PHASES: tuple[PhaseSpec, ...] = (
    PhaseSpec("kernel", "Kernel initialized", True, 2.0),
    PhaseSpec("filesystem", "Filesystem mounted", True, 10.0),
    PhaseSpec("security", "Security initialized", True, 30.0, "security"),
    PhaseSpec("lan", "LAN initialized", False, 15.0),
    PhaseSpec("database", "Database started", True, 60.0, "database"),
    PhaseSpec("backend", "Backend started", True, 60.0, "backend"),
    PhaseSpec("backup", "Backup scheduler started", False, 20.0, "backup"),
    PhaseSpec("monitor", "Monitoring initialized", True, 45.0, "monitor"),
)


class BootReporter:
    def __init__(
        self,
        settings: Settings,
        *,
        terminal: Terminal | None = None,
        systemd: Systemd | None = None,
        logger: EtaLogger | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        health_checker: Callable[..., Mapping[str, HealthResult]] | None = None,
        authenticator: Any | None = None,
        maintenance_factory: Callable[..., Any] | None = None,
        lan_checker: Callable[[], tuple[str, str]] | None = None,
        kernel_cmdline: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.terminal = terminal or Terminal()
        self.systemd = systemd or Systemd()
        self.logger = logger or EtaLogger("boot", settings.log_dir, run_dir=settings.run_dir, clock=clock)
        self.service_logger = EtaLogger("services", settings.log_dir, run_dir=settings.run_dir, clock=clock)
        self.clock = clock
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.health_checker = health_checker or collect_health
        self.authenticator = authenticator
        self.maintenance_factory = maintenance_factory
        self.lan_checker = lan_checker or self._lan_status
        self.kernel_cmdline = kernel_cmdline if kernel_cmdline is not None else _read_cmdline()
        self.environ = environ if environ is not None else os.environ
        self.interrupted = False
        self.results: list[PhaseResult] = []
        self._started = monotonic()
        self._deadline = self._started + float(settings.get("boot.timeout_seconds", 180))
        self.overall = "unknown"

    # -- interruption / maintenance -----------------------------------------
    def request_interrupt(self) -> None:
        self.interrupted = True

    def _maintenance_requested_by_kernel(self) -> bool:
        return "eta.maintenance=1" in self.kernel_cmdline or "eta.maintenance=rescue" in self.kernel_cmdline

    def _enter_maintenance(self) -> None:
        self.terminal.blank()
        self.terminal.line("Maintenance mode requested. Services keep running.", YELLOW)
        self.terminal.line("This only pauses the boot display; systemd is not interrupted.", DIM)
        require_auth = bool(self.settings.get("maintenance.require_auth", True))
        if require_auth:
            auth = self.authenticator or authenticator_from_settings(self.settings, self.logger)
            attempts = int(self.settings.get("maintenance.max_auth_attempts", 3))
            try:
                acquired = auth.authenticate_interactive(
                    attempts=attempts,
                    stream=self.terminal.streams[0] if self.terminal.streams else None,
                )
            except AuthUnavailable as exc:
                self.terminal.line(f"Authentication unavailable: {exc}", RED)
                self.logger.error(
                    f"Maintenance authentication unavailable: {exc}",
                    event="maintenance_auth_unavailable",
                )
                return
            if not acquired:
                self.terminal.line("Access denied. Continuing normal boot.", RED)
                self.logger.security(
                    "Maintenance mode denied after failed authentication",
                    event="maintenance_denied",
                )
                return
            self.logger.security("Maintenance mode granted", event="maintenance_granted")
        factory = self.maintenance_factory
        if factory is None:
            from .maintenance import MaintenanceSession

            factory = MaintenanceSession
        self.terminal.line("Entering maintenance environment. Type 'help' for commands.", CYAN)
        try:
            session = factory(settings=self.settings, logger=self.logger, systemd=self.systemd)
            session.run()
        except Exception as exc:
            self.terminal.line(f"Maintenance session error: {exc}", RED)
            self.logger.error(f"Maintenance session error: {exc}", event="maintenance_error")
        finally:
            self.logger.security("Maintenance mode exited", event="maintenance_exit")
        self.terminal.line("Returning to the boot sequence.", CYAN)
        self.terminal.blank()

    def _check_interrupt(self) -> None:
        if not self.interrupted:
            return
        self.interrupted = False
        self._enter_maintenance()

    # -- phase checks -------------------------------------------------------
    def _lan_status(self) -> tuple[str, str]:
        target = self.systemd.show("network-online.target")
        if target.active:
            return "ok", "LAN online"
        try:
            names = [p.name for p in Path("/sys/class/net").iterdir() if p.name != "lo"]
        except OSError:
            names = []
        carriers = []
        for name in names:
            try:
                carrier = Path(f"/sys/class/net/{name}/carrier").read_text(encoding="utf-8").strip()
                state = Path(f"/sys/class/net/{name}/operstate").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if carrier == "1" or state == "up":
                carriers.append(name)
        if carriers:
            return "warn", f"link up on {', '.join(carriers)}; no routable LAN yet"
        if names:
            return "warn", "no LAN link; offline mode"
        return "warn", "no network interface; offline mode"

    def _check_phase(self, phase: PhaseSpec) -> tuple[str, str, dict[str, Any], int | None]:
        if phase.key == "kernel":
            return "ok", f"Linux {platform.release()} ({platform.machine()})", {}, None
        if phase.key == "filesystem":
            state = self.systemd.show("local-fs.target")
            if state.active:
                return "ok", "local-fs.target active", {}, None
            return "fail", f"local-fs.target {state.summary()}", {}, state.exit_code
        if phase.key == "lan":
            status, detail = self.lan_checker()
            return status, detail, {}, None
        if phase.key == "security":
            if not service_enabled(self.settings, "security"):
                return "skip", "disabled by configuration", {}, None
        assert phase.service is not None
        result = self.health_checker([phase.service], systemd=self.systemd)[phase.service]
        data = dict(result.data)
        state = self.systemd.show(get_spec(phase.service).unit)
        exit_code = state.exit_code
        if result.ok:
            return "ok", result.detail, data, exit_code
        return ("fail" if phase.required else "warn"), result.detail, data, exit_code

    def _poll_phase(self, phase: PhaseSpec, title_for_pending: bool) -> PhaseResult:
        started = self.monotonic()
        phase_deadline = min(started + phase.timeout, self._deadline)
        if title_for_pending:
            self.terminal.pending(phase.title)
        last_detail = "not checked"
        last_status = "fail"
        last_data: dict[str, Any] = {}
        exit_code: int | None = None
        first = True
        while True:
            self._check_interrupt()
            if phase.key == "database" and not service_enabled(self.settings, "database"):
                status, detail, data = "skip", "disabled by configuration", {}
                break
            if phase.key == "backend" and not service_enabled(self.settings, "backend"):
                status, detail, data = "skip", "disabled by configuration", {}
                break
            if phase.key == "backup" and not service_enabled(self.settings, "backup"):
                status, detail, data = "skip", "disabled by configuration", {}
                break
            if phase.key == "monitor" and not service_enabled(self.settings, "monitor"):
                status, detail, data = "skip", "disabled by configuration", {}
                break
            try:
                status, detail, data, exit_code = self._check_phase(phase)
            except Exception as exc:
                status, detail, data, exit_code = "fail", f"check error: {exc}", {}, None
            last_status, last_detail, last_data = status, detail, data
            if status in {"ok", "skip", "warn"}:
                break
            if not first:
                pass
            first = False
            now = self.monotonic()
            if now >= phase_deadline or now >= self._deadline:
                detail = f"timed out after {format_duration(now - started)}: {detail}"
                last_detail = detail
                break
            self._check_interrupt()
            self.sleeper(min(0.5, max(0.0, phase_deadline - now)))
        duration = self.monotonic() - started
        result = PhaseResult(
            key=phase.key,
            title=phase.title,
            status=last_status,
            detail=last_detail,
            duration=duration,
            required=phase.required,
            exit_code=exit_code,
            recovery=dict(last_data.get("recovery", {}) or {}),
        )
        self.terminal.status(result.status, result.title, result.duration, result.detail)
        if result.status == "fail":
            self.logger.error_record(
                f"{result.title} failed: {result.detail}",
                event="boot_phase_failed",
                phase=result.key,
                service=phase.service,
                exit_code=result.exit_code,
                recovery_attempt=result.recovery.get("attempt_count"),
                recovery_outcome=result.recovery.get("detail") or result.recovery.get("action"),
                duration_seconds=round(result.duration, 3),
            )
            self.service_logger.error_record(
                f"{result.title} failed during boot: {result.detail}",
                event="service_boot_failed",
                service=phase.service,
                exit_code=result.exit_code,
                recovery_attempt=result.recovery.get("attempt_count"),
                recovery_outcome=result.recovery.get("detail") or result.recovery.get("action"),
            )
        elif result.status == "warn":
            self.logger.warning(
                f"{result.title}: {result.detail}",
                event="boot_phase_warning",
                phase=result.key,
                service=phase.service,
            )
        return result

    # -- run ----------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        self.logger.info(
            "Boot display started",
            event="boot_start",
            eta_version=__version__,
            upstream=UPSTREAM,
            uptime_seconds=round(uptime_seconds(self.clock), 1),
        )
        self.terminal.write(BANNER)
        self.terminal.line(f"{PRODUCT} boot sequence", BOLD)
        self.terminal.blank()
        started_wall = self.clock()
        for phase in PHASES:
            if self.monotonic() >= self._deadline:
                result = PhaseResult(
                    phase.key,
                    phase.title,
                    "fail",
                    "boot display time budget exhausted",
                    0.0,
                    phase.required,
                )
                self.terminal.status(result.status, result.title, result.duration, result.detail)
                self.logger.error_record(
                    f"{result.title} not checked: boot time budget exhausted",
                    event="boot_phase_failed",
                    phase=result.key,
                    service=phase.service,
                    recovery_outcome="not attempted",
                )
                self.results.append(result)
                continue
            self.results.append(self._poll_phase(phase, title_for_pending=phase.key not in {"kernel", "filesystem"}))
            self._check_interrupt()

        # Final health collection is broader than the display phases.
        try:
            final_results = self.health_checker(None, systemd=self.systemd)
        except Exception as exc:
            final_results = {}
            self.logger.error(f"Final health collection failed: {exc}", event="boot_health_error")
        self.overall = overall_state(self.settings, final_results) if final_results else "degraded"
        if any(not result.ok for result in self.results if result.required):
            self.overall = "degraded"
        try:
            write_health_state(
                self.settings,
                final_results,
                clock=self.clock,
                extra={"boot_display": True, "overall": self.overall},
            )
        except OSError:
            pass
        self._summary(final_results, started_wall)

        if self._maintenance_requested_by_kernel():
            self.terminal.line("Kernel requested maintenance mode.", YELLOW)
            self._enter_maintenance()
            self._summary(final_results, started_wall)

        return {
            "overall": self.overall,
            "phases": [result.as_dict() for result in self.results],
            "duration_seconds": round(self.clock() - started_wall, 3),
        }

    def _summary(self, final_results: Mapping[str, HealthResult], started_wall: float) -> None:
        duration = self.clock() - started_wall
        self.terminal.blank()
        self.terminal.line("System health summary", BOLD)
        for result in self.results:
            self.terminal.status(result.status, result.title, result.duration, result.detail)
        failed = [result for result in self.results if result.status == "fail"]
        if failed:
            self.terminal.blank()
            self.terminal.line("Failed components:", RED)
            for result in failed:
                self.terminal.line(f"  - {result.title}: {result.detail}", RED)
            self.terminal.line("Inspect: eta-admin status / eta-admin logs errors", DIM)
        if final_results:
            for key, result in sorted(final_results.items()):
                if not result.ok and result.status != "skipped":
                    if not any(phase.service == key and phase.status == "fail" for phase in self.results):
                        self.terminal.line(f"  - {key}: {result.detail}", RED)
        self.terminal.blank()
        if self.overall == "ready":
            self.terminal.line("ETA OS IS READY.", f"{GREEN}{BOLD}")
        else:
            self.terminal.line("ETA OS IS DEGRADED", f"{YELLOW}{BOLD}")
            self.terminal.line("Services keep running. Use the maintenance mode or eta-admin to repair.", YELLOW)
        self.terminal.line(f"Boot completed in {format_duration(duration)}", DIM)
        self.terminal.blank()
        self.terminal.line(IDENTITY_LINE, CYAN)
        self.terminal.line("Minimal. Offline. Reliable. Controlled.", DIM)
        self.terminal.blank()
        summary = {
            "schema": "eta.boot.summary.v1",
            "overall": self.overall,
            "ready": self.overall == "ready",
            "phases": [result.as_dict() for result in self.results],
            "duration_seconds": round(duration, 3),
            "eta_version": __version__,
            "upstream": UPSTREAM,
            "creator": "mohamedboukerch55",
        }
        self.logger.log(
            "info",
            "Boot completed",
            event="boot_summary",
            overall=self.overall,
            ready=self.overall == "ready",
            phases=summary["phases"],
            duration_seconds=summary["duration_seconds"],
        )
        try:
            atomic_write_json(self.settings.run_dir / "boot-summary.json", summary, mode=0o644)
        except OSError:
            pass


def _read_cmdline() -> str:
    try:
        return Path("/proc/cmdline").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _open_serial(path: str) -> TextIO | None:
    try:
        if not Path(path).exists():
            return None
        fd = os.open(path, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
        return os.fdopen(fd, "w", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eta-boot", description="Eta OS boot display")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--serial", default=None, help="additional serial console device")
    parser.add_argument("--maintenance", action="store_true", help="force maintenance mode after boot")
    parser.add_argument("--timeout", type=float, default=None)
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"eta-boot: configuration error: {exc}", file=sys.stderr)
        return 2
    if args.timeout is not None:
        settings.raw["boot"]["timeout_seconds"] = args.timeout
    streams: list[TextIO] = [sys.stdout]
    serial_path = args.serial or os.environ.get("ETA_BOOT_SERIAL")
    if serial_path is None and bool(settings.get("boot.serial_console", True)):
        serial_path = "/dev/ttyS0"
    if serial_path:
        serial = _open_serial(serial_path)
        if serial is not None:
            streams.append(serial)
    terminal = Terminal(streams, color=False if args.no_color else None)
    reporter = BootReporter(settings, terminal=terminal, kernel_cmdline=_read_cmdline() if not args.maintenance else "eta.maintenance=1")
    term_state = None
    if sys.stdin.isatty():
        try:
            term_state = termios.tcgetattr(sys.stdin.fileno())
        except termios.error:
            term_state = None

    def handle_sigint(_signum, _frame):
        reporter.request_interrupt()

    previous = signal.signal(signal.SIGINT, handle_sigint)
    try:
        reporter.run()
    except KeyboardInterrupt:
        reporter.request_interrupt()
        reporter._enter_maintenance()
    finally:
        signal.signal(signal.SIGINT, previous)
        if term_state is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, term_state)
            except termios.error:
                pass
        for stream in streams:
            try:
                stream.flush()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
