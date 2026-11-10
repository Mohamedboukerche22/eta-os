"""Master administrator authentication.

Eta OS deliberately does not implement password hashing or password storage.
Interactive authentication goes through PAM (``pam_unix`` unless the
administrator configures something stronger). Privileged CLI operations go
through sudo, which also uses PAM. This module only adds rate limiting and audit
logging around PAM.

There are no hardcoded credentials and no password is ever logged.
"""

from __future__ import annotations

import getpass
import os
import time
from pathlib import Path
from typing import Callable, TextIO

from .logger import EtaLogger
from .util import ConfigError, atomic_write_json, read_json, utc_iso

MAX_FAILURES_DEFAULT = 5
WINDOW_DEFAULT = 300
LOCKOUT_DEFAULT = 300


class AuthError(Exception):
    pass


class AuthUnavailable(AuthError):
    pass


class MasterAuthenticator:
    def __init__(
        self,
        username: str = "eta-master",
        *,
        pam_service: str = "eta-admin",
        state_dir: str | os.PathLike[str] = "/var/lib/eta",
        logger: EtaLogger | None = None,
        verifier: Callable[[str, str, str], bool] | None = None,
        clock: Callable[[], float] = time.time,
        max_failures: int = MAX_FAILURES_DEFAULT,
        failure_window: int = WINDOW_DEFAULT,
        lockout_seconds: int = LOCKOUT_DEFAULT,
    ) -> None:
        self.username = username
        self.pam_service = pam_service
        self.state_path = Path(state_dir) / "auth-lockout.json"
        self.logger = logger or EtaLogger("security", "/var/log/eta")
        self._verifier = verifier
        self.clock = clock
        self.max_failures = max(1, int(max_failures))
        self.failure_window = max(1, int(failure_window))
        self.lockout_seconds = max(1, int(lockout_seconds))

    # -- state --------------------------------------------------------------
    def _load(self) -> dict:
        data = read_json(self.state_path, {})
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        try:
            atomic_write_json(self.state_path, data, mode=0o600)
        except OSError:
            # Never block authentication because the state file is unwritable;
            # PAM's own faillock (when enabled) still applies.
            pass

    def _user_state(self, data: dict, username: str) -> dict:
        users = data.setdefault("users", {})
        state = users.setdefault(username, {})
        if not isinstance(state, dict):
            state = {}
            users[username] = state
        return state

    def locked(self, username: str | None = None) -> tuple[bool, float]:
        username = username or self.username
        now = self.clock()
        data = self._load()
        state = data.get("users", {}).get(username, {})
        if not isinstance(state, dict):
            return False, 0.0
        locked_until = float(state.get("locked_until", 0) or 0)
        if locked_until > now:
            return True, locked_until - now
        return False, 0.0

    def _register_failure(self, username: str) -> None:
        now = self.clock()
        data = self._load()
        state = self._user_state(data, username)
        failures = [
            float(ts)
            for ts in state.get("failures", [])
            if isinstance(ts, (int, float)) and now - float(ts) <= self.failure_window
        ]
        failures.append(now)
        state["failures"] = failures[-self.max_failures :]
        state["last_failure"] = utc_iso(now)
        if len(failures) >= self.max_failures:
            state["locked_until"] = now + self.lockout_seconds
            self.logger.critical(
                "Master authentication temporarily locked after repeated failures",
                event="auth_lockout",
                user=username,
                lockout_seconds=self.lockout_seconds,
            )
        self._save(data)

    def _register_success(self, username: str) -> None:
        data = self._load()
        users = data.get("users", {})
        if isinstance(users, dict):
            users.pop(username, None)
        self._save(data)

    # -- verification -------------------------------------------------------
    def _pam_verify(self, username: str, password: str, service: str) -> bool:
        try:
            import pam  # type: ignore
        except ImportError as exc:
            raise AuthUnavailable(
                "python-pam is not installed; refuse to fall back to an insecure verifier"
            ) from exc
        try:
            authenticator = pam.pam()
            return bool(authenticator.authenticate(username, password, service=service))
        except Exception as exc:
            raise AuthUnavailable(f"PAM authentication error: {exc}") from exc

    def verify(self, password: str, username: str | None = None) -> bool:
        username = username or self.username
        verifier = self._verifier or self._pam_verify
        try:
            return bool(verifier(username, password, self.pam_service))
        except AuthUnavailable:
            raise
        except Exception as exc:
            raise AuthUnavailable(f"authentication backend failed: {exc}") from exc

    def authenticate(self, password: str, username: str | None = None) -> bool:
        username = username or self.username
        if not password:
            self.logger.security(
                "Empty master password rejected",
                event="auth_failure",
                user=username,
                reason="empty_password",
            )
            return False
        locked, remaining = self.locked(username)
        if locked:
            self.logger.security(
                "Authentication refused while locked out",
                event="auth_locked",
                user=username,
                remaining_seconds=round(remaining, 1),
            )
            return False
        try:
            ok = self.verify(password, username)
        except AuthUnavailable as exc:
            self.logger.error(
                f"Authentication unavailable: {exc}",
                event="auth_unavailable",
                user=username,
            )
            raise
        if ok:
            self._register_success(username)
            self.logger.security(
                "Master authentication succeeded",
                event="auth_success",
                user=username,
            )
            return True
        self._register_failure(username)
        self.logger.warning(
            "Master authentication failed",
            event="auth_failure",
            user=username,
        )
        return False

    def authenticate_interactive(
        self,
        *,
        attempts: int = 3,
        prompt: str = "Master password: ",
        input_func: Callable[[str], str] | None = None,
        stream: TextIO | None = None,
        username: str | None = None,
    ) -> bool:
        username = username or self.username
        ask = input_func or getpass.getpass
        for attempt in range(1, max(1, attempts) + 1):
            try:
                password = ask(prompt)
            except (EOFError, KeyboardInterrupt):
                if stream is not None:
                    stream.write("\nAuthentication cancelled.\n")
                return False
            locked, remaining = self.locked(username)
            if locked:
                if stream is not None:
                    stream.write(f"Locked out. Try again in {int(remaining) + 1}s.\n")
                self.logger.security(
                    "Interactive authentication refused while locked out",
                    event="auth_locked",
                    user=username,
                )
                return False
            try:
                if self.authenticate(password, username):
                    return True
            except AuthUnavailable as exc:
                if stream is not None:
                    stream.write(f"Authentication unavailable: {exc}\n")
                return False
            if attempt < attempts and stream is not None:
                stream.write(f"Authentication failed ({attempt}/{attempts}).\n")
            del password
        return False


def require_root() -> None:
    if os.geteuid() != 0:
        raise AuthError("this operation requires root (use sudo)")


def current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return str(os.getuid())


def authenticator_from_settings(settings, logger: EtaLogger | None = None) -> MasterAuthenticator:
    try:
        return MasterAuthenticator(
            settings.master_user,
            pam_service=str(settings.get("auth.pam_service", "eta-admin")),
            state_dir=settings.state_dir,
            logger=logger,
            max_failures=int(settings.get("auth.max_failures", MAX_FAILURES_DEFAULT)),
            failure_window=int(settings.get("auth.failure_window_seconds", WINDOW_DEFAULT)),
            lockout_seconds=int(settings.get("auth.lockout_seconds", LOCKOUT_DEFAULT)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid authentication settings: {exc}") from exc
