"""Persistent JSON Lines logging for Eta OS.

Logs are written to ``/var/log/eta`` by the process that owns the event. The
same records are visible in journald through the service's stdout/stderr. If a
log file cannot be written (full disk, permissions) the record falls back to the
runtime directory and then to stderr; critical errors are never silently lost.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO

from .util import ConfigError, ensure_dir, read_jsonl, utc_iso

SEVERITY_LEVELS = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "critical": 50,
}

LOG_FILES = {
    "boot": "boot.log",
    "services": "services.log",
    "system": "services.log",
    "backend": "backend.log",
    "database": "database.log",
    "backup": "backup.log",
    "security": "security.log",
    "errors": "errors.log",
    "recovery": "recovery.log",
    "monitor": "monitor.log",
    "admin": "admin.log",
    "install": "install.log",
}
ERROR_SEVERITIES = {"error", "critical"}
MAX_MESSAGE = 8192


def component_filename(component: str) -> str:
    if component in LOG_FILES:
        return LOG_FILES[component]
    if not component or not all(ch.isalnum() or ch in "-_" for ch in component):
        return LOG_FILES["errors"]
    return f"{component}.log"


class EtaLogger:
    """A tiny, dependency-free structured logger."""

    def __init__(
        self,
        component: str,
        log_dir: str | os.PathLike[str] | None = None,
        *,
        run_dir: str | os.PathLike[str] | None = None,
        clock: Callable[[], float] = time.time,
        echo: Callable[[dict[str, Any]], None] | None = None,
        stderr: TextIO | None = None,
        fsync_errors: bool = True,
    ) -> None:
        self.component = component
        self.log_dir = Path(log_dir or os.environ.get("ETA_LOG_DIR", "/var/log/eta"))
        self.run_dir = Path(run_dir or os.environ.get("ETA_RUN_DIR", "/run/eta"))
        self.clock = clock
        self.echo = echo
        self.stderr = stderr if stderr is not None else sys.stderr
        self.fsync_errors = fsync_errors
        self.fallbacks: list[str] = []
        self._host = os.uname().nodename if hasattr(os, "uname") else "unknown"

    # -- public API ---------------------------------------------------------
    def debug(self, message: str, **fields: Any) -> dict[str, Any]:
        return self.log("debug", message, **fields)

    def info(self, message: str, **fields: Any) -> dict[str, Any]:
        return self.log("info", message, **fields)

    def warning(self, message: str, **fields: Any) -> dict[str, Any]:
        return self.log("warning", message, **fields)

    def error(self, message: str, **fields: Any) -> dict[str, Any]:
        return self.log("error", message, **fields)

    def critical(self, message: str, **fields: Any) -> dict[str, Any]:
        return self.log("critical", message, **fields)

    def log(self, severity: str, message: str, **fields: Any) -> dict[str, Any]:
        severity = severity.lower()
        if severity not in SEVERITY_LEVELS:
            raise ValueError(f"invalid severity: {severity}")
        record: dict[str, Any] = {
            "schema": "eta.log.v1",
            "ts": utc_iso(self.clock()),
            "component": self.component,
            "severity": severity,
            "message": str(message)[:MAX_MESSAGE],
            "pid": os.getpid(),
            "host": self._host,
        }
        if severity in ERROR_SEVERITIES:
            record.setdefault("exit_code", fields.pop("exit_code", None))
            record.setdefault("recovery_attempt", fields.pop("recovery_attempt", None))
            record.setdefault("recovery_outcome", fields.pop("recovery_outcome", None))
        for key, value in fields.items():
            if key in record:
                continue
            try:
                json.dumps(value)
                record[key] = value
            except (TypeError, ValueError):
                record[key] = repr(value)
        self._write(record, severity)
        if self.echo is not None:
            try:
                self.echo(record)
            except Exception:  # display must never break logging
                pass
        return record

    def error_record(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        recovery_attempt: int | str | None = None,
        recovery_outcome: str | None = None,
        severity: str = "error",
        **fields: Any,
    ) -> dict[str, Any]:
        return self.log(
            severity,
            message,
            exit_code=exit_code,
            recovery_attempt=recovery_attempt,
            recovery_outcome=recovery_outcome,
            **fields,
        )

    def security(self, message: str, **fields: Any) -> dict[str, Any]:
        record = self.log("info", message, **fields)
        if self.component != "security":
            self._write_to(self.log_dir / LOG_FILES["security"], record, "info")
        return record

    # -- internals ----------------------------------------------------------
    def _paths_for(self, severity: str) -> list[Path]:
        paths = [self.log_dir / component_filename(self.component)]
        if severity in ERROR_SEVERITIES:
            errors_path = self.log_dir / LOG_FILES["errors"]
            if errors_path not in paths:
                paths.append(errors_path)
        return paths

    def _write(self, record: dict[str, Any], severity: str) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for path in self._paths_for(severity):
            if not self._write_to(path, record, severity, line):
                self._fallback(path, line)

    def _write_to(
        self,
        path: Path,
        record: dict[str, Any],
        severity: str,
        line: str | None = None,
    ) -> bool:
        payload = line or json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            ensure_dir(path.parent, 0o750)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, payload.encode("utf-8"))
            if self.fsync_errors and severity in ERROR_SEVERITIES:
                os.fsync(fd)
            return True
        except OSError:
            return False
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _fallback(self, original: Path, line: str) -> None:
        fallback_dir = self.run_dir / "eta-logs"
        self.fallbacks.append(str(original))
        try:
            ensure_dir(fallback_dir, 0o750)
            path = fallback_dir / original.name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            return
        except OSError:
            pass
        try:
            self.stderr.write("ETA-LOG-FALLBACK " + line)
            self.stderr.flush()
        except Exception:
            pass


def read_log(
    component: str,
    *,
    lines: int = 200,
    log_dir: str | os.PathLike[str] | None = None,
    min_severity: str | None = None,
) -> list[dict[str, Any]]:
    directory = Path(log_dir or os.environ.get("ETA_LOG_DIR", "/var/log/eta"))
    records = read_jsonl(directory / component_filename(component), tail=None)
    if min_severity:
        threshold = SEVERITY_LEVELS.get(min_severity.lower(), 0)
        records = [r for r in records if SEVERITY_LEVELS.get(str(r.get("severity")), 0) >= threshold]
    return records[-lines:] if lines >= 0 else records


def tail_file(path: str | os.PathLike[str], lines: int) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return list(deque(handle, maxlen=max(0, lines)))
    except OSError:
        return []


def read_boot_summary(log_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    directory = Path(log_dir or os.environ.get("ETA_LOG_DIR", "/var/log/eta"))
    import json as _json

    try:
        with open(directory / "boot.log", "r", encoding="utf-8") as handle:
            rows = [line for line in handle if line.strip()]
        for line in reversed(rows):
            try:
                record = _json.loads(line)
            except ValueError:
                continue
            if record.get("event") == "boot_summary":
                return record
    except OSError:
        pass
    return {}


def rotate_file(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backups: int = 5,
) -> bool:
    """Size-based rotation used by tests and ``eta-admin logs rotate``.

    NixOS also installs a logrotate rule; this function is a safe fallback for
    environments where logrotate is not installed (recovery media).
    """
    target = Path(path)
    try:
        if target.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    for index in range(backups - 1, 0, -1):
        src = target.with_name(f"{target.name}.{index}")
        dst = target.with_name(f"{target.name}.{index + 1}")
        if src.exists():
            os.replace(src, dst)
    os.replace(target, target.with_name(f"{target.name}.1"))
    try:
        os.chmod(target.with_name(f"{target.name}.1"), 0o640)
    except OSError:
        pass
    return True


def validate_component(component: str) -> str:
    if component not in LOG_FILES and not all(ch.isalnum() or ch in "-_" for ch in component):
        raise ConfigError(f"invalid log component: {component!r}")
    return component
