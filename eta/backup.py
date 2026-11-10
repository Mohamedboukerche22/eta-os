"""Backup and restore using restic.

Design points:

* SQLite is snapshotted with the online backup API; the live file is never
  copied directly.
* MariaDB is dumped with ``--single-transaction`` and never by copying its
  datadir.
* restic runs with low CPU/IO priority (the systemd unit also sets Nice=19 and
  idle IO scheduling).
* A lock file prevents overlapping runs.
* Every run writes an atomic status file used by the CLI, monitor and boot UI.
* Restores default to a staging directory under ``backup.restore_root``. There is
  no implicit "restore over /" path.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import __version__
from .config import Settings, load_settings
from .database import (
    DatabaseError,
    check_sqlite,
    mariadb_dump,
    mariadb_ready,
    sqlite_snapshot,
)
from .logger import EtaLogger
from .util import (
    CommandResult,
    EtaError,
    Runner,
    SNAPSHOT_RE,
    atomic_write_json,
    ensure_dir,
    file_mode,
    human_bytes,
    read_json,
    run,
    utc_iso,
)


class BackupError(EtaError):
    pass


@dataclass
class ResticResult:
    ok: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def output(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


def _json_lines(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


class Restic:
    def __init__(
        self,
        settings: Settings,
        *,
        runner: Runner = run,
        binary: str | None = None,
        clock: Callable[[], float] = time.time,
        low_priority: bool = True,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.binary = binary or os.environ.get("ETA_RESTIC") or shutil.which("restic") or ""
        self.clock = clock
        self.low_priority = low_priority
        self.repository = str(settings.get("backup.repository"))
        self.password_file = str(settings.get("backup.password_file"))
        self.cache_dir = str(settings.state_dir / "restic-cache")

    def _env(self) -> dict[str, str]:
        return {
            "RESTIC_REPOSITORY": self.repository,
            "RESTIC_PASSWORD_FILE": self.password_file,
            "RESTIC_CACHE_DIR": self.cache_dir,
            "RESTIC_PROGRESS_FPS": "0",
            "LC_ALL": "C",
        }

    def _argv(self, args: Iterable[str]) -> list[str]:
        argv = [self.binary or "restic", *args]
        if not self.low_priority:
            return argv
        nice = shutil.which("nice")
        ionice = shutil.which("ionice")
        if ionice:
            argv = [ionice, "-c3", "-n7", *argv]
        if nice:
            argv = [nice, "-n", "19", *argv]
        return argv

    def run(self, args: Iterable[str], *, timeout: float = 6 * 3600) -> ResticResult:
        if not self.binary:
            raise BackupError("restic is not installed")
        result = self.runner(self._argv(list(args)), timeout=timeout, env=self._env())
        return ResticResult(result.ok, result.returncode, result.stdout, result.stderr)

    def available(self) -> bool:
        return bool(self.binary) and Path(self.binary).exists() or bool(shutil.which(self.binary or ""))


class BackupManager:
    def __init__(
        self,
        settings: Settings,
        *,
        logger: EtaLogger | None = None,
        restic: Restic | None = None,
        clock: Callable[[], float] = time.time,
        lock_path: str | Path | None = None,
        hostname: str | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger or EtaLogger("backup", settings.log_dir, run_dir=settings.run_dir, clock=clock)
        self.restic = restic or Restic(settings, clock=clock)
        self.clock = clock
        self.lock_path = Path(lock_path or (settings.run_dir / "backup.lock"))
        self.hostname = hostname or os.uname().nodename
        self.repository = Path(str(settings.get("backup.repository")))
        self.password_file = Path(str(settings.get("backup.password_file")))
        self.status_file = Path(str(settings.get("backup.status_file")))
        self.snapshot_root = Path(str(settings.get("backup.snapshot_dir")))
        self.restore_root = Path(str(settings.get("backup.restore_root")))
        self._lock_fd: int | None = None

    # -- preflight ----------------------------------------------------------
    def preflight(self) -> list[str]:
        errors: list[str] = []
        if not self.restic.binary:
            errors.append("restic binary not found")
        if not self.password_file.is_file():
            errors.append(f"restic password file missing: {self.password_file}")
        else:
            mode = file_mode(self.password_file)
            if mode & 0o077:
                errors.append(f"restic password file must be 0600/0400 (is {oct(mode)})")
            if self.password_file.stat().st_size == 0:
                errors.append("restic password file is empty")
            if os.geteuid() == 0 and self.password_file.stat().st_uid != 0:
                errors.append("restic password file must be owned by root")
        if not str(self.repository).startswith("/"):
            errors.append("backup repository must be an absolute path")
        parent = self.repository
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if parent.exists() and not os.access(parent, os.W_OK):
            errors.append(f"backup repository parent is not writable: {parent}")
        try:
            usage = shutil.disk_usage(parent if parent.exists() else Path("/"))
            minimum = int(self.settings.get("backup.min_free_bytes", 512 * 1024 * 1024))
            if usage.free < minimum:
                errors.append(
                    f"not enough free space for backups: {human_bytes(usage.free)} < {human_bytes(minimum)}"
                )
        except OSError:
            pass
        if not str(self.restore_root).startswith("/"):
            errors.append("restore root must be absolute")
        return errors

    # -- lock ---------------------------------------------------------------
    def _acquire_lock(self) -> bool:
        ensure_dir(self.lock_path.parent, 0o750)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {utc_iso(self.clock())}\n".encode())
            os.fsync(fd)
            self._lock_fd = fd
            return True
        except OSError:
            os.close(fd)
            return False

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    # -- status -------------------------------------------------------------
    def _write_status(self, payload: dict[str, Any]) -> None:
        existing = read_json(self.status_file, {})
        if isinstance(existing, dict) and existing.get("outcome") in {"success", "failed"}:
            payload.setdefault("last_success", existing.get("last_success"))
            if existing.get("outcome") == "success" and not payload.get("last_success"):
                payload["last_success"] = {
                    "last_end": existing.get("last_end"),
                    "snapshot_id": existing.get("snapshot_id"),
                }
            if existing.get("outcome") == "success":
                payload["last_success"] = {
                    "last_end": existing.get("last_end"),
                    "snapshot_id": existing.get("snapshot_id"),
                }
        payload["schema"] = "eta.backup.v1"
        payload["repository"] = str(self.repository)
        payload["updated_at"] = utc_iso(self.clock())
        try:
            atomic_write_json(self.status_file, payload, mode=0o644)
        except OSError as exc:
            self.logger.error(f"cannot write backup status: {exc}", event="backup_status_error")

    def status(self) -> dict[str, Any]:
        state = read_json(self.status_file, {})
        if not isinstance(state, dict):
            state = {}
        state.setdefault("schema", "eta.backup.v1")
        state.setdefault("outcome", "never")
        state["repository"] = str(self.repository)
        state["repository_exists"] = self.repository.is_dir()
        state["password_file"] = str(self.password_file)
        state["password_file_mode"] = oct(file_mode(self.password_file)) if self.password_file.exists() else None
        state["running"] = self._lock_is_held()
        return state

    def _lock_is_held(self) -> bool:
        try:
            fd = os.open(self.lock_path, os.O_RDONLY)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except OSError:
            return True
        finally:
            os.close(fd)

    # -- repository ---------------------------------------------------------
    def ensure_repository(self) -> None:
        if not self.restic.binary:
            raise BackupError("restic is not installed")
        probes = self.restic.run(["snapshots", "--json", "--last", "1"], timeout=60)
        if probes.ok:
            return
        text = (probes.stderr + probes.stdout).lower()
        missing_markers = (
            "does not exist",
            "unable to open config",
            "is there a repository",
            "no repository",
            "config file does not exist",
            "repository does not exist",
        )
        if not any(marker in text for marker in missing_markers):
            raise BackupError(f"cannot open restic repository: {probes.output[:500]}")
        ensure_dir(self.repository, 0o700)
        init = self.restic.run(["init"], timeout=120)
        if not init.ok:
            raise BackupError(f"restic init failed: {init.output[:500]}")
        self.logger.info("Initialized restic repository", event="backup_repo_init", repository=str(self.repository))

    # -- run ----------------------------------------------------------------
    def run(self, *, reason: str = "scheduled") -> dict[str, Any]:
        errors = self.preflight()
        if errors:
            message = "; ".join(errors)
            self.logger.error(message, event="backup_preflight_failed")
            self._write_status(
                {
                    "outcome": "failed",
                    "reason": reason,
                    "last_start": utc_iso(self.clock()),
                    "last_start_epoch": self.clock(),
                    "last_end": utc_iso(self.clock()),
                    "error": message,
                }
            )
            raise BackupError(message)
        if not self._acquire_lock():
            self.logger.warning("Backup already running; skipping", event="backup_locked")
            return {"outcome": "locked", "detail": "another backup is running"}
        started = self.clock()
        started_iso = utc_iso(started)
        self._write_status(
            {
                "outcome": "running",
                "reason": reason,
                "last_start": started_iso,
                "last_start_epoch": started,
                "pid": os.getpid(),
            }
        )
        self.logger.info("Backup started", event="backup_start", reason=reason)
        snapshot_dir: Path | None = None
        try:
            snapshot_dir = self._make_snapshot_dir(started)
            manifest = self._prepare_data(snapshot_dir)
            self.ensure_repository()
            summary = self._restic_backup(snapshot_dir)
            snapshot_id = summary.get("snapshot_id") or summary.get("id")
            retention = self._forget()
            verification = self._verify()
            ended = self.clock()
            status = {
                "outcome": "success",
                "reason": reason,
                "last_start": started_iso,
                "last_start_epoch": started,
                "last_end": utc_iso(ended),
                "last_end_epoch": ended,
                "last_success": {"last_end": utc_iso(ended), "snapshot_id": snapshot_id},
                "duration_seconds": round(ended - started, 3),
                "snapshot_id": snapshot_id,
                "files_new": summary.get("files_new"),
                "files_changed": summary.get("files_changed"),
                "files_unmodified": summary.get("files_unmodified"),
                "bytes_added": summary.get("data_added"),
                "total_bytes_processed": summary.get("total_bytes_processed"),
                "total_files_processed": summary.get("total_files_processed"),
                "verify": verification,
                "retention": retention,
                "manifest": manifest,
            }
            self._write_status(status)
            self.logger.info(
                "Backup complete",
                event="backup_success",
                snapshot_id=snapshot_id,
                duration_seconds=status["duration_seconds"],
                bytes_added=status["bytes_added"],
                verify=verification.get("outcome"),
            )
            if snapshot_dir is not None:
                self._cleanup_snapshots(keep=2)
            return status
        except Exception as exc:
            ended = self.clock()
            text = f"{type(exc).__name__}: {exc}"
            self._write_status(
                {
                    "outcome": "failed",
                    "reason": reason,
                    "last_start": started_iso,
                    "last_start_epoch": started,
                    "last_end": utc_iso(ended),
                    "last_end_epoch": ended,
                    "duration_seconds": round(ended - started, 3),
                    "error": text[:1500],
                    "snapshot_staging": str(snapshot_dir) if snapshot_dir else None,
                }
            )
            self.logger.error(
                f"Backup failed: {text}",
                event="backup_failed",
                reason=reason,
                staging=str(snapshot_dir) if snapshot_dir else None,
            )
            if snapshot_dir is not None:
                self._cleanup_snapshots(keep=3)
            raise
        finally:
            self._release_lock()

    def _make_snapshot_dir(self, started: float) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(started))
        path = self.snapshot_root / stamp
        if path.exists():
            path = self.snapshot_root / f"{stamp}-{os.getpid()}"
        ensure_dir(path, 0o700)
        return path

    def _prepare_data(self, snapshot_dir: Path) -> dict[str, Any]:
        database_dir = snapshot_dir / "database"
        ensure_dir(database_dir, 0o700)
        manifest: dict[str, Any] = {
            "schema": "eta.backup.manifest.v1",
            "eta_version": __version__,
            "hostname": self.hostname,
            "created_at": utc_iso(self.clock()),
            "database_engine": self.settings.db_engine,
            "database_files": [],
            "warnings": [],
        }
        if self.settings.db_engine == "sqlite":
            source = self.settings.sqlite_path
            if source.is_file():
                target = database_dir / "eta.sqlite"
                sqlite_snapshot(source, target)
                manifest["database_files"].append("database/eta.sqlite")
                self.logger.info("SQLite snapshot created", event="backup_sqlite_snapshot", bytes=target.stat().st_size)
            else:
                manifest["warnings"].append(f"SQLite database missing: {source}")
                self.logger.warning(
                    f"SQLite database missing during backup: {source}", event="backup_db_missing"
                )
        elif self.settings.db_engine == "mariadb":
            ok, detail = mariadb_ready(self.settings)
            if ok:
                try:
                    target = mariadb_dump(self.settings, database_dir / "mariadb.sql", self.logger)
                    if target:
                        manifest["database_files"].append("database/mariadb.sql")
                except DatabaseError as exc:
                    raise BackupError(str(exc)) from exc
            else:
                manifest["warnings"].append(f"MariaDB not ready; logical dump skipped: {detail}")
                self.logger.warning(
                    f"MariaDB not ready; backup continues without a database dump: {detail}",
                    event="backup_db_skip",
                )
        (snapshot_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return manifest

    def _restic_backup(self, snapshot_dir: Path) -> dict[str, Any]:
        paths: list[str] = [str(snapshot_dir)]
        for configured in self.settings.get("backup.paths", []):
            path = Path(str(configured))
            if path.exists():
                paths.append(str(path))
            else:
                self.logger.warning(f"backup path does not exist: {path}", event="backup_path_missing")
        args = ["backup", "--json", "--tag", "eta", "--host", self.hostname, "--one-file-system"]
        for pattern in self.settings.get("backup.excludes", []):
            args.extend(["--exclude", str(pattern)])
        args.extend(paths)
        result = self.restic.run(args)
        if not result.ok:
            raise BackupError(f"restic backup failed ({result.returncode}): {result.output[:800]}")
        summaries = [row for row in _json_lines(result.stdout) if row.get("message_type") == "summary"]
        summary = summaries[-1] if summaries else {}
        return summary

    def _forget(self) -> dict[str, Any]:
        args = ["forget", "--json"]
        for key in ("keep_daily", "keep_weekly", "keep_monthly", "keep_yearly"):
            args.extend([f"--{key.replace('_', '-')}", str(int(self.settings.get(f"backup.{key}", 0)))])
        args.append("--prune")
        result = self.restic.run(args, timeout=6 * 3600)
        if not result.ok:
            self.logger.warning(
                f"restic forget/prune failed: {result.output[:400]}",
                event="backup_forget_failed",
            )
            return {"outcome": "failed", "detail": result.output[:400]}
        payload = _parse_json(result.stdout)
        return {"outcome": "ok", "result": payload if payload is not None else result.stdout[-400:]}

    def _verify(self) -> dict[str, Any]:
        subset = str(self.settings.get("backup.verify_subset", "5%"))
        result = self.restic.run(["check", "--read-data-subset", subset, "--json"], timeout=6 * 3600)
        if not result.ok:
            self.logger.error(
                f"Backup verification failed: {result.output[:400]}",
                event="backup_verify_failed",
            )
            return {"outcome": "failed", "subset": subset, "detail": result.output[:400]}
        self.logger.info("Backup verification passed", event="backup_verified", subset=subset)
        return {"outcome": "ok", "subset": subset}

    def _cleanup_snapshots(self, *, keep: int = 2) -> None:
        try:
            dirs = sorted([p for p in self.snapshot_root.iterdir() if p.is_dir()], key=lambda p: p.name)
        except OSError:
            return
        for old in dirs[:-keep] if keep > 0 else dirs:
            if old.resolve().parent != self.snapshot_root.resolve():
                continue
            try:
                shutil.rmtree(old)
            except OSError:
                pass

    # -- restore ------------------------------------------------------------
    def list_snapshots(self, limit: int = 20) -> list[dict[str, Any]]:
        result = self.restic.run(["snapshots", "--json"], timeout=120)
        if not result.ok:
            raise BackupError(f"cannot list snapshots: {result.output[:500]}")
        payload = _parse_json(result.stdout)
        rows = payload if isinstance(payload, list) else []
        rows = [row for row in rows if isinstance(row, dict)]
        rows.sort(key=lambda row: str(row.get("time", "")), reverse=True)
        return rows[:limit]

    def _validate_target(self, target: Path | None) -> Path:
        root = self.restore_root.resolve()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.clock()))
        resolved = (target or (root / stamp)).resolve()
        if resolved == Path("/") or resolved in {Path("/etc"), Path("/var"), Path("/usr"), Path("/boot")}:
            raise BackupError("refusing to restore over a system directory")
        if root not in resolved.parents and resolved != root:
            raise BackupError(f"restore target must be inside {root}")
        return resolved

    def restore(
        self,
        snapshot: str = "latest",
        target: str | Path | None = None,
        *,
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        if snapshot != "latest" and not SNAPSHOT_RE.match(snapshot):
            raise BackupError("invalid snapshot id")
        resolved = self._validate_target(Path(target) if target else None)
        if resolved.exists() and any(resolved.iterdir()) and not overwrite:
            raise BackupError(f"restore target is not empty: {resolved}")
        ensure_dir(resolved, 0o700)
        args = ["restore", snapshot, "--target", str(resolved), "--json"]
        if dry_run:
            args.append("--dry-run")
        result = self.restic.run(args, timeout=6 * 3600)
        if not result.ok:
            raise BackupError(f"restore failed ({result.returncode}): {result.output[:800]}")
        verification: dict[str, Any] = {"outcome": "skipped"}
        sqlite_file = resolved / "database" / "eta.sqlite"
        if sqlite_file.is_file() and not dry_run:
            ok, detail = check_sqlite(sqlite_file, quick=True)
            verification = {"outcome": "ok" if ok else "failed", "detail": detail}
            if not ok:
                self.logger.error(
                    f"Restored SQLite database failed integrity check: {detail}",
                    event="restore_verify_failed",
                    target=str(resolved),
                )
        payload = {
            "schema": "eta.restore.v1",
            "outcome": "success",
            "snapshot": snapshot,
            "target": str(resolved),
            "dry_run": dry_run,
            "verification": verification,
            "restored_at": utc_iso(self.clock()),
        }
        self.logger.info(
            "Restore complete",
            event="restore_success",
            snapshot=snapshot,
            target=str(resolved),
            dry_run=dry_run,
        )
        return payload


def backup_status(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    manager = BackupManager(settings)
    return manager.status()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="eta-backup", description="Eta OS restic backup manager")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="run a backup now")
    run_parser.add_argument("--reason", default="manual")
    status_parser = sub.add_parser("status", help="show backup status")
    status_parser.add_argument("--json", action="store_true")
    list_parser = sub.add_parser("list", help="list snapshots")
    list_parser.add_argument("--json", action="store_true")
    restore_parser = sub.add_parser("restore", help="restore a snapshot to the staging area")
    restore_parser.add_argument("--snapshot", default="latest")
    restore_parser.add_argument("--target")
    restore_parser.add_argument("--dry-run", action="store_true")
    restore_parser.add_argument("--overwrite", action="store_true")
    restore_parser.add_argument("--yes", action="store_true", help="required non-interactively")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"eta-backup: configuration error: {exc}")
        return 2
    try:
        if args.command == "status":
            payload = backup_status(settings)
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"outcome:    {payload.get('outcome')}")
                print(f"repository: {payload.get('repository')} (exists={payload.get('repository_exists')})")
                if payload.get("last_success"):
                    print(f"last success: {payload['last_success'].get('last_end')} {payload['last_success'].get('snapshot_id') or ''}")
                if payload.get("last_end"):
                    print(f"last end:   {payload['last_end']}")
                if payload.get("error"):
                    print(f"error:      {payload['error']}")
            return 0
        if args.command == "list":
            manager = BackupManager(settings)
            rows = manager.list_snapshots()
            if args.json:
                print(json.dumps(rows, indent=2, sort_keys=True))
            else:
                for row in rows:
                    print(f"{row.get('short_id') or row.get('id')}  {row.get('time')}  {','.join(row.get('tags') or [])}")
            return 0
        if args.command == "restore":
            manager = BackupManager(settings)
            if not args.yes:
                print("eta-backup: refusing restore without --yes (or use eta-admin backup restore)")
                return 2
            payload = manager.restore(
                args.snapshot,
                args.target,
                dry_run=args.dry_run,
                overwrite=args.overwrite,
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        if args.command == "run":
            manager = BackupManager(settings)
            payload = manager.run(reason=args.reason)
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
            return 0 if payload.get("outcome") == "success" else 1
    except Exception as exc:
        print(f"eta-backup: {type(exc).__name__}: {exc}")
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
