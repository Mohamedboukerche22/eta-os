"""Eta OS backend API.

A deliberately small, dependency-free HTTP service that demonstrates the real
database dependency. Liveness (``/healthz``) says the process is alive.
Readiness (``/readyz``) verifies the configured database every time, and returns
503 when the database is unavailable. The process keeps running and systemd keeps
it supervised; it never pretends to be operational.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import CREATOR, IDENTITY_LINE, PRODUCT, UPSTREAM, __version__
from .config import Settings, load_settings
from .database import DatabaseError, MariaDBStore, SQLiteStore, connect_sqlite, validate_key
from .logger import EtaLogger
from .sdnotify import ready as sd_ready
from .sdnotify import status as sd_status
from .sdnotify import stopping as sd_stopping
from .sdnotify import watchdog as sd_watchdog
from .util import utc_iso

MAX_VALUE_BYTES = 64 * 1024


class BackendStore:
    """Store wrapper that refuses to create a missing database."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = settings.db_engine
        self._store: SQLiteStore | MariaDBStore | None = None
        self._last_error: str | None = None

    def _open(self) -> SQLiteStore | MariaDBStore:
        if self.engine == "sqlite":
            path = self.settings.sqlite_path
            if not path.is_file():
                raise DatabaseError(
                    f"SQLite database not initialized: {path} (eta-database.service owns initialization)"
                )
            return SQLiteStore(path)
        if self.engine == "mariadb":
            store = MariaDBStore(self.settings)
            store.ensure_schema()
            return store
        raise DatabaseError(f"unknown database engine: {self.engine}")

    def store(self) -> SQLiteStore | MariaDBStore:
        if self._store is None:
            try:
                self._store = self._open()
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                raise
        return self._store

    def reset(self) -> None:
        self._store = None

    def ping(self) -> None:
        try:
            self.store().ping()
        except Exception:
            self.reset()
            raise

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def get(self, key: str) -> str | None:
        return self.store().get(key)

    def put(self, key: str, value: str) -> None:
        self.store().put(key, value)

    def delete(self, key: str) -> bool:
        return self.store().delete(key)


class EtaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, handler, backend: "EtaBackend") -> None:
        self.backend = backend
        self.slots = threading.BoundedSemaphore(max(1, backend.max_workers))
        super().__init__(address, handler)

    def process_request(self, request, client_address):  # type: ignore[override]
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 27\r\nConnection: close\r\n\r\n"
                    b'{"error":"server busy"}\n'
                )
            finally:
                self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):  # type: ignore[override]
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class EtaBackend:
    def __init__(
        self,
        settings: Settings,
        *,
        logger: EtaLogger | None = None,
        store: BackendStore | None = None,
        clock: Callable[[], float] = time.time,
        notifier: Callable[[str], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logger or EtaLogger("backend", settings.log_dir, run_dir=settings.run_dir, clock=clock)
        self.store = store or BackendStore(settings)
        self.clock = clock
        self.notifier = notifier
        self.max_workers = int(settings.get("backend.max_workers", 4))
        self.max_body = int(settings.get("backend.max_body_bytes", MAX_VALUE_BYTES))
        self._server: EtaHTTPServer | None = None
        self._stop = threading.Event()
        self._watchdog_stop = threading.Event()

    def _notify(self, state: str) -> None:
        if self.notifier is not None:
            try:
                self.notifier(state)
            except Exception:
                pass

    def create_server(self, bind: str | None = None, port: int | None = None) -> EtaHTTPServer:
        address = (bind or self.settings.backend_bind, int(port if port is not None else self.settings.backend_port))
        server = EtaHTTPServer(address, EtaRequestHandler, self)
        self._server = server
        return server

    def _watchdog_loop(self) -> None:
        try:
            usec = int(os.environ.get("WATCHDOG_USEC", "0"))
        except ValueError:
            usec = 0
        if usec <= 0:
            return
        interval = max(1.0, usec / 3_000_000)
        while not self._watchdog_stop.wait(interval):
            self._notify("WATCHDOG=1")

    def serve_forever(self, bind: str | None = None, port: int | None = None) -> int:
        server = self.create_server(bind, port)
        host, actual_port = server.server_address[:2]
        self.logger.info(
            "Backend listening",
            event="backend_listen",
            bind=host,
            port=actual_port,
            database=self.settings.db_engine,
            eta_version=__version__,
        )
        self._notify("READY=1")
        sd_ready()
        sd_status(f"ETA OS backend listening on {host}:{actual_port}")
        watchdog_thread = threading.Thread(target=self._watchdog_loop, name="eta-watchdog", daemon=True)
        watchdog_thread.start()

        def stop(_signum, _frame):
            self.logger.info("Backend stopping", event="backend_stopping")
            sd_stopping()
            self._stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        old_term = signal.signal(signal.SIGTERM, stop)
        old_int = signal.signal(signal.SIGINT, stop)
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            self._watchdog_stop.set()
            server.server_close()
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
            self.logger.info("Backend stopped", event="backend_stopped")
        return 0

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            threading.Thread(target=self._server.shutdown, daemon=True).start()

    # -- request helpers ----------------------------------------------------
    def readiness(self) -> tuple[bool, str, dict[str, Any]]:
        started = time.monotonic()
        try:
            self.store.ping()
            elapsed = round((time.monotonic() - started) * 1000, 2)
            return True, "ready", {"engine": self.settings.db_engine, "ping_ms": elapsed}
        except Exception as exc:
            detail = str(exc)
            self.logger.warning(
                f"Database readiness failed: {detail}",
                event="backend_not_ready",
                database=self.settings.db_engine,
            )
            return False, detail, {"engine": self.settings.db_engine}

    def info(self) -> dict[str, Any]:
        ready, detail, data = self.readiness()
        return {
            "schema": "eta.backend.info.v1",
            "product": PRODUCT,
            "eta_version": __version__,
            "upstream": UPSTREAM,
            "creator": CREATOR,
            "identity": IDENTITY_LINE,
            "hostname": os.uname().nodename,
            "uptime_seconds": round(_uptime(), 1),
            "database": {"ready": ready, "detail": detail, **data},
            "endpoints": ["/healthz", "/readyz", "/api/v1/info", "/api/v1/kv/<key>"],
        }


class EtaRequestHandler(BaseHTTPRequestHandler):
    server_version = "eta-backend/" + __version__
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def backend(self) -> EtaBackend:
        return self.server.backend  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # silence stdlib stderr logging
        return

    # -- plumbing -----------------------------------------------------------
    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self._log_request(status)

    def _log_request(self, status: int) -> None:
        duration = round((time.monotonic() - getattr(self, "_started", time.monotonic())) * 1000, 2)
        self.backend.logger.info(
            "HTTP request",
            event="http_request",
            method=self.command,
            path=self.path.split("?", 1)[0][:200],
            status=status,
            duration_ms=duration,
            remote=self.client_address[0] if self.client_address else None,
        )

    def _body(self) -> tuple[dict[str, Any] | None, int, str]:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            return None, 411, "Content-Length required"
        try:
            length = int(length_header)
        except ValueError:
            return None, 400, "invalid Content-Length"
        if length < 0 or length > self.backend.max_body:
            return None, 413, f"body too large (max {self.backend.max_body} bytes)"
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, 400, "body must be JSON"
        if not isinstance(value, dict):
            return None, 400, "body must be a JSON object"
        return value, 200, ""

    # -- routes -------------------------------------------------------------
    def do_GET(self) -> None:
        self._started = time.monotonic()
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            self._send(200, {"status": "alive", "service": "eta-backend", "version": __version__})
            return
        if path == "/readyz":
            ready, detail, data = self.backend.readiness()
            if ready:
                self._send(200, {"status": "ready", "database": data})
            else:
                self._send(503, {"status": "degraded", "error": detail, "database": data})
            return
        if path in {"/", "/api/v1/info"}:
            self._send(200, self.backend.info())
            return
        if path.startswith("/api/v1/kv/"):
            key = urllib.parse.unquote(path[len("/api/v1/kv/"):])
            try:
                validate_key(key)
            except DatabaseError as exc:
                self._send(400, {"error": str(exc)})
                return
            try:
                value = self.backend.store.get(key)
            except Exception as exc:
                self._send(503, {"error": f"database unavailable: {exc}"})
                return
            if value is None:
                self._send(404, {"error": "not found", "key": key})
            else:
                self._send(200, {"key": key, "value": value})
            return
        self._send(404, {"error": "not found"})

    def do_PUT(self) -> None:
        self._started = time.monotonic()
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/v1/kv/"):
            self._send(404, {"error": "not found"})
            return
        key = urllib.parse.unquote(path[len("/api/v1/kv/"):])
        try:
            validate_key(key)
        except DatabaseError as exc:
            self._send(400, {"error": str(exc)})
            return
        body, status, error = self._body()
        if body is None:
            self._send(status, {"error": error})
            return
        value = body.get("value")
        if not isinstance(value, str):
            self._send(400, {"error": "body must contain a string 'value'"})
            return
        if len(value.encode("utf-8")) > self.backend.max_body:
            self._send(413, {"error": "value too large"})
            return
        try:
            self.backend.store.put(key, value)
        except Exception as exc:
            self._send(503, {"error": f"database unavailable: {exc}"})
            return
        self._send(200, {"key": key, "stored": True})

    def do_DELETE(self) -> None:
        self._started = time.monotonic()
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/v1/kv/"):
            self._send(404, {"error": "not found"})
            return
        key = urllib.parse.unquote(path[len("/api/v1/kv/"):])
        try:
            validate_key(key)
        except DatabaseError as exc:
            self._send(400, {"error": str(exc)})
            return
        try:
            deleted = self.backend.store.delete(key)
        except Exception as exc:
            self._send(503, {"error": f"database unavailable: {exc}"})
            return
        self._send(200 if deleted else 404, {"key": key, "deleted": deleted})

    def do_POST(self) -> None:
        self._started = time.monotonic()
        self._send(405, {"error": "method not allowed"})

    def do_HEAD(self) -> None:
        self._started = time.monotonic()
        path = urllib.parse.urlsplit(self.path).path
        if path in {"/healthz", "/readyz", "/", "/api/v1/info"}:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


def _uptime() -> float:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError):
        return 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eta-backend", description="Eta OS backend API")
    parser.add_argument("--bind", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"eta-backend: configuration error: {exc}", file=sys.stderr)
        return 2
    backend = EtaBackend(settings)
    return backend.serve_forever(args.bind, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
