"""Database helpers: SQLite (default) and optional MariaDB.

Eta OS never backs up a live database file directly. SQLite snapshots use the
sqlite3 online backup API; MariaDB dumps use ``--single-transaction``.
"""

from __future__ import annotations

import grp
import os
import re
import secrets
import shutil
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .logger import EtaLogger
from .util import CommandResult, ensure_dir, file_mode, run

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS eta_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)

KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class DatabaseError(Exception):
    pass


def _sqlite_uri(path: Path, read_only: bool) -> str:
    if read_only:
        return f"file:{path}?mode=ro"
    return str(path)


def connect_sqlite(path: Path | str, *, read_only: bool = False, timeout: float = 5.0) -> sqlite3.Connection:
    target = Path(path)
    if not read_only:
        ensure_dir(target.parent, 0o750)
    conn = sqlite3.connect(_sqlite_uri(target, read_only), timeout=timeout, uri=read_only)
    conn.row_factory = sqlite3.Row
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_sqlite(path: Path | str) -> None:
    conn = connect_sqlite(path)
    try:
        for statement in SCHEMA:
            conn.execute(statement)
        conn.commit()
        conn.execute(
            "INSERT INTO eta_meta(key, value) VALUES('schema_version', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        conn.commit()
    finally:
        conn.close()


def check_sqlite(path: Path | str, *, quick: bool = False) -> tuple[bool, str]:
    target = Path(path)
    if not target.is_file():
        return False, f"database file missing: {target}"
    try:
        conn = connect_sqlite(target, read_only=True, timeout=3.0)
        try:
            conn.execute("SELECT 1").fetchone()
            if quick:
                row = conn.execute("PRAGMA quick_check").fetchone()
                if not row or row[0] != "ok":
                    return False, f"quick_check failed: {row[0] if row else 'no result'}"
            count = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='eta_meta'"
            ).fetchone()[0]
            if count != 1:
                return False, "eta_meta table missing"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"sqlite error: {exc}"
    return True, "sqlite ok"


def sqlite_snapshot(source: Path | str, destination: Path | str) -> str:
    """Create a transactionally consistent snapshot using the backup API."""
    src = Path(source)
    dst = Path(destination)
    if not src.is_file():
        raise DatabaseError(f"database file missing: {src}")
    ensure_dir(dst.parent, 0o750)
    if dst.exists():
        dst.unlink()
    src_conn = connect_sqlite(src, read_only=True)
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn, pages=256, sleep=0.01)
        dst_conn.commit()
        row = dst_conn.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            raise DatabaseError(f"snapshot integrity check failed: {row[0] if row else 'no result'}")
        dst_conn.execute("PRAGMA journal_mode=DELETE")
        dst_conn.commit()
    finally:
        try:
            dst_conn.close()
        finally:
            src_conn.close()
    os.chmod(dst, 0o600)
    return str(dst)


def _mariadb_binary(name: str) -> str | None:
    for candidate in (name, name.replace("mariadb", "mysql")):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def mariadb_ready(settings: Settings, *, app_credentials: bool = False, timeout: float = 5.0) -> tuple[bool, str]:
    """Check the MariaDB socket. Uses app credentials when requested."""
    socket_path = str(settings.get("database.mariadb_socket", "/run/mysqld/mysqld.sock"))
    if not Path(socket_path).exists():
        return False, f"mariadb socket missing: {socket_path}"
    if app_credentials:
        try:
            store = MariaDBStore(settings)
            store.ping()
            return True, "mariadb app connection ok"
        except Exception as exc:  # connector/import/auth errors are all "not ready"
            return False, f"mariadb app connection failed: {exc}"
    binary = _mariadb_binary("mariadb-admin")
    if binary:
        result = run(
            [binary, "--protocol=socket", f"--socket={socket_path}", "ping"],
            timeout=timeout,
        )
        if result.returncode == 0:
            return True, "mariadb ping ok"
        return False, (result.output or "mariadb ping failed")[:300]
    # Last resort: socket exists. The monitor will still verify the unit state.
    return True, "mariadb socket present (client tools unavailable)"


def mariadb_dump(settings: Settings, destination: Path | str, logger: EtaLogger) -> Path | None:
    """Dump all configured MariaDB databases consistently."""
    socket_path = str(settings.get("database.mariadb_socket", "/run/mysqld/mysqld.sock"))
    if not Path(socket_path).exists():
        logger.warning("MariaDB dump skipped: socket not present", event="backup_db_skip")
        return None
    binary = _mariadb_binary("mariadb-dump")
    if not binary:
        raise DatabaseError("mariadb-dump not found; cannot take a consistent dump")
    options = Path(str(settings.get("database.mariadb_backup_options")))
    argv = [binary, "--protocol=socket", f"--socket={socket_path}", "--single-transaction",
            "--quick", "--skip-lock-tables", "--routines", "--events", "--triggers"]
    if options.is_file():
        mode = file_mode(options)
        if mode & 0o077:
            raise DatabaseError(f"{options} must not be group/world accessible (mode {oct(mode)})")
        argv.insert(1, f"--defaults-extra-file={options}")
    database = str(settings.get("database.mariadb_database", "eta"))
    argv.append(database)
    target = Path(destination)
    ensure_dir(target.parent, 0o750)
    with open(target, "wb") as handle:
        import subprocess

        proc = subprocess.run(argv, stdout=handle, stderr=subprocess.PIPE, timeout=1800)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")[:500]
        raise DatabaseError(f"mariadb-dump failed ({proc.returncode}): {stderr}")
    os.chmod(target, 0o600)
    logger.info(
        "MariaDB logical dump complete",
        event="backup_db_dump",
        database=database,
        bytes=target.stat().st_size,
    )
    return target


def database_health(settings: Settings) -> tuple[bool, str]:
    engine = settings.db_engine
    if engine == "sqlite":
        return check_sqlite(settings.sqlite_path)
    if engine == "mariadb":
        return mariadb_ready(settings)
    return False, f"unknown database engine: {engine}"


def _chown_eta_group(path: Path, *, directory: bool = False) -> None:
    """Give the backend group access without making the path world-accessible."""
    try:
        gid = grp.getgrnam("eta-backend").gr_gid
    except KeyError:
        return
    try:
        os.chown(path, 0, gid)
    except OSError:
        return
    try:
        os.chmod(path, 0o770 if directory else 0o660)
    except OSError:
        pass


def init_database(settings: Settings, logger: EtaLogger) -> None:
    engine = settings.db_engine
    if engine == "sqlite":
        db_path = Path(settings.sqlite_path)
        ensure_dir(db_path.parent, 0o770)
        _chown_eta_group(db_path.parent, directory=True)
        init_sqlite(db_path)
        _chown_eta_group(db_path)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                _chown_eta_group(sidecar)
        logger.info("SQLite database ready", event="database_init", path=str(db_path))
        return
    if engine != "mariadb":
        raise DatabaseError(f"unknown database engine: {engine}")
    deadline = time.monotonic() + 60
    last = "not checked"
    while time.monotonic() < deadline:
        ok, last = mariadb_ready(settings)
        if ok:
            init_mariadb(settings, logger)
            return
        time.sleep(1)
    raise DatabaseError(f"MariaDB did not become ready: {last}")


def init_mariadb(settings: Settings, logger: EtaLogger) -> None:
    """Create the Eta application database and a generated unix-socket user.

    MariaDB is optional. The application password is generated locally and kept
    in a 0600 file; it is never placed in the Nix store or in logs.
    """
    binary = _mariadb_binary("mariadb")
    if not binary:
        raise DatabaseError("mariadb client not found")
    socket_path = str(settings.get("database.mariadb_socket", "/run/mysqld/mysqld.sock"))
    key_path = Path(str(settings.get("database.mariadb_app_password_file", "/var/lib/eta/mariadb-app.key")))
    ensure_dir(key_path.parent, 0o750)
    if key_path.is_file():
        if file_mode(key_path) & 0o077:
            raise DatabaseError(f"{key_path} permissions are too open")
        password = key_path.read_text(encoding="utf-8").strip()
    else:
        password = secrets.token_urlsafe(32)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(password + "\n")
        os.chmod(key_path, 0o600)
    database = str(settings.get("database.mariadb_database", "eta"))
    user = str(settings.get("database.mariadb_user", "eta"))
    safe_db = re.sub(r"[^A-Za-z0-9_]", "", database)
    safe_user = re.sub(r"[^A-Za-z0-9_]", "", user)
    if not safe_db or not safe_user:
        raise DatabaseError("invalid MariaDB database or user name")
    safe_password = password.replace("\\", "\\\\").replace("'", "\\'")
    sql = (
        f"CREATE DATABASE IF NOT EXISTS `{safe_db}`; "
        f"CREATE USER IF NOT EXISTS '{safe_user}'@'localhost' IDENTIFIED BY '{safe_password}'; "
        f"ALTER USER '{safe_user}'@'localhost' IDENTIFIED BY '{safe_password}'; "
        f"GRANT ALL PRIVILEGES ON `{safe_db}`.* TO '{safe_user}'@'localhost'; "
        "FLUSH PRIVILEGES;"
    )
    result = run(
        [binary, "--protocol=socket", f"--socket={socket_path}", "-uroot", "-e", sql],
        timeout=30,
    )
    if result.returncode != 0:
        raise DatabaseError(f"MariaDB initialization failed: {result.output[:500]}")
    backup_options = Path(str(settings.get("database.mariadb_backup_options", "/etc/eta/mariadb-backup.cnf")))
    ensure_dir(backup_options.parent, 0o755)
    fd = os.open(backup_options, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(
            "[client]\n"
            f"user={safe_user}\n"
            f"password={password}\n"
            f"socket={socket_path}\n"
        )
    os.chmod(backup_options, 0o600)
    logger.info("MariaDB application database ready", event="database_init", database=safe_db)


class MariaDBStore:
    """Application key/value store backed by MariaDB (optional path)."""

    def __init__(self, settings: Settings) -> None:
        try:
            import mysql.connector  # type: ignore
        except ImportError as exc:
            raise DatabaseError("mysql-connector is not installed; MariaDB store unavailable") from exc
        key_path = Path(str(settings.get("database.mariadb_app_password_file", "/var/lib/eta/mariadb-app.key")))
        if not key_path.is_file():
            raise DatabaseError(f"MariaDB application password missing: {key_path}")
        password = key_path.read_text(encoding="utf-8").strip()
        self._password = password
        self._connector = mysql.connector
        self._settings = settings
        self._database = str(settings.get("database.mariadb_database", "eta"))
        self._user = str(settings.get("database.mariadb_user", "eta"))

    def _connect(self):
        return self._connector.connect(
            unix_socket=str(self._settings.get("database.mariadb_socket", "/run/mysqld/mysqld.sock")),
            user=self._user,
            password=self._password,
            database=self._database,
            connection_timeout=3,
            autocommit=True,
        )

    def ping(self) -> None:
        conn = self._connect()
        try:
            conn.cursor().execute("SELECT 1")
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS kv (k VARCHAR(128) PRIMARY KEY, v TEXT NOT NULL, updated_at VARCHAR(32) NOT NULL)"
            )
        finally:
            conn.close()

    def get(self, key: str) -> str | None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT v FROM kv WHERE k=%s", (key,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def put(self, key: str, value: str) -> None:
        from .util import utc_iso

        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO kv(k, v, updated_at) VALUES(%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE v=VALUES(v), updated_at=VALUES(updated_at)",
                (key, value, utc_iso()),
            )
        finally:
            conn.close()

    def delete(self, key: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM kv WHERE k=%s", (key,))
            return cur.rowcount > 0
        finally:
            conn.close()


class SQLiteStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        init_sqlite(self.path)

    def ping(self) -> None:
        conn = connect_sqlite(self.path, read_only=True)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        init_sqlite(self.path)

    def get(self, key: str) -> str | None:
        conn = connect_sqlite(self.path, read_only=True)
        try:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def put(self, key: str, value: str) -> None:
        from .util import utc_iso

        conn = connect_sqlite(self.path)
        try:
            conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, utc_iso()),
            )
            conn.commit()
        finally:
            conn.close()

    def delete(self, key: str) -> bool:
        conn = connect_sqlite(self.path)
        try:
            cur = conn.execute("DELETE FROM kv WHERE key=?", (key,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def open_store(settings: Settings) -> SQLiteStore | MariaDBStore:
    if settings.db_engine == "sqlite":
        return SQLiteStore(settings.sqlite_path)
    if settings.db_engine == "mariadb":
        store = MariaDBStore(settings)
        store.ensure_schema()
        return store
    raise DatabaseError(f"unknown database engine: {settings.db_engine}")


def validate_key(key: str) -> str:
    if not KEY_RE.match(key or ""):
        raise DatabaseError("invalid key; use 1-128 characters of A-Z a-z 0-9 . _ -")
    return key


def main(argv: list[str] | None = None) -> int:
    """CLI used by eta-database.service."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="eta-db-init", description="Initialize or check the Eta OS database")
    parser.add_argument("--wait", type=float, default=0.0, help="wait up to N seconds for MariaDB")
    parser.add_argument("--check", action="store_true", help="check only")
    parser.add_argument("--snapshot", metavar="DEST", help="create a consistent SQLite snapshot at DEST")
    args = parser.parse_args(argv)
    try:
        from .config import load_settings
        from .logger import EtaLogger

        settings = load_settings()
        logger = EtaLogger("database", settings.log_dir, run_dir=settings.run_dir)
    except Exception as exc:
        print(f"eta-db-init: configuration error: {exc}", file=sys.stderr)
        return 2
    if args.snapshot:
        try:
            sqlite_snapshot(settings.sqlite_path, args.snapshot)
        except Exception as exc:
            logger.error(f"snapshot failed: {exc}", event="database_snapshot_failed")
            print(f"eta-db-init: {exc}", file=sys.stderr)
            return 1
        logger.info("SQLite snapshot created", event="database_snapshot", destination=args.snapshot)
        return 0
    if args.check:
        ok, detail = database_health(settings)
        print(detail)
        return 0 if ok else 1
    try:
        if settings.db_engine == "mariadb" and args.wait > 0:
            deadline = time.monotonic() + args.wait
            while time.monotonic() < deadline:
                ok, detail = mariadb_ready(settings)
                if ok:
                    break
                time.sleep(1)
        init_database(settings, logger)
    except Exception as exc:
        logger.error(f"database initialization failed: {exc}", event="database_init_failed")
        print(f"eta-db-init: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
