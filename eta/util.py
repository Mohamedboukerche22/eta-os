"""Small shared helpers: atomic files, clocks, process execution, validation."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}$")
SERVICE_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SNAPSHOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class EtaError(Exception):
    """Base error for the Eta OS management layer."""


class ConfigError(EtaError):
    """Invalid or unsafe configuration."""


class CommandError(EtaError):
    """A required external command failed."""

    def __init__(self, argv: Sequence[str], returncode: int, output: str = "") -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.output = output
        super().__init__(f"{' '.join(self.argv)} exited with {returncode}")


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


Runner = Callable[..., CommandResult]


def run(
    argv: Sequence[str],
    *,
    timeout: float = 30.0,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    input_text: str | None = None,
    check: bool = False,
) -> CommandResult:
    """Run a command without a shell. Never use shell=True in Eta OS code."""
    args = list(argv)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        proc = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=full_env,
            cwd=cwd,
            check=False,
        )
        result = CommandResult(args, proc.returncode, proc.stdout or "", proc.stderr or "")
    except FileNotFoundError as exc:
        result = CommandResult(args, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        result = CommandResult(args, 124, exc.stdout or "", stderr or "timeout")
    if check and not result.ok:
        raise CommandError(args, result.returncode, result.output)
    return result


def which(name: str, fallback: str | None = None) -> str | None:
    return shutil.which(name) or fallback


def utc_now_iso(clock: Callable[[], float] = time.time) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock()))


def utc_iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"not a boolean: {value!r}")


def atomic_write(path: Path | str, data: str | bytes, *, mode: int = 0o600) -> None:
    """Write via a temporary file in the same directory, fsync, then rename."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        try:
            dir_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path | str, data: Any, *, mode: int = 0o600) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=mode)


def read_json(path: Path | str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def read_jsonl(path: Path | str, *, tail: int | None = None) -> list[dict[str, Any]]:
    """Read JSON Lines, skipping malformed lines instead of crashing."""
    records: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except OSError:
        return []
    if tail is not None and tail >= 0:
        return records[-tail:] if tail else []
    return records


def file_mode(path: Path | str) -> int:
    return os.stat(path).st_mode & 0o777


def ensure_dir(path: Path | str, mode: int = 0o750) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target, mode)
    except OSError:
        pass
    return target


def check_safe_unit(unit: str) -> str:
    if not isinstance(unit, str) or not UNIT_RE.match(unit) or "/" in unit or ".." in unit:
        raise EtaError(f"unsafe unit name: {unit!r}")
    return unit


def check_safe_name(name: str) -> str:
    if not isinstance(name, str) or not SERVICE_KEY_RE.match(name):
        raise EtaError(f"unsafe service name: {name!r}")
    return name


def check_hostname(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 63 or not HOSTNAME_RE.match(name):
        raise EtaError("invalid hostname; use letters, digits and hyphens (max 63 chars)")
    return name.lower()


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, rest = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{rest:02d}s"


def human_bytes(value: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    return f"{value:.1f}{units[index]}" if index else f"{int(value)}{units[index]}"


def boot_time() -> float:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except OSError:
        pass
    return time.time()


def uptime_seconds(clock: Callable[[], float] = time.time) -> float:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError):
        return max(0.0, clock() - boot_time())


def iter_sorted(values: Iterable[str]) -> list[str]:
    return sorted(values)
