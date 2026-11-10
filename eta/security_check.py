"""Boot-time security baseline enforcement.

This is intentionally boring: fix ownership/modes of Eta's own files, verify the
master account exists, verify the firewall is active and warn about risky
configuration. It never rewrites NixOS configuration.
"""

from __future__ import annotations

import argparse
import grp
import os
import pwd
import stat
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings, load_settings
from .logger import EtaLogger
from .services import Systemd
from .util import ensure_dir


class SecurityChecker:
    def __init__(self, settings: Settings, *, logger: EtaLogger | None = None, systemd: Systemd | None = None) -> None:
        self.settings = settings
        self.logger = logger or EtaLogger("security", settings.log_dir, run_dir=settings.run_dir)
        self.systemd = systemd or Systemd()
        self.issues: list[dict[str, Any]] = []

    def _add(self, level: str, message: str, **fields: Any) -> None:
        self.issues.append({"level": level, "message": message, **fields})
        log = {"ok": self.logger.info, "info": self.logger.info, "warn": self.logger.warning, "critical": self.logger.critical}.get(
            level, self.logger.info
        )
        log(message, event="security_check", level=level, **fields)

    def _fix(self, path: Path, mode: int, *, owner: str | None = None, group: str | None = None) -> None:
        try:
            if not path.exists():
                return
            current = stat.S_IMODE(path.stat().st_mode)
            if current != mode:
                os.chmod(path, mode)
                self._add("info", f"fixed permissions on {path}", path=str(path), before=oct(current), after=oct(mode))
            if os.geteuid() == 0 and (owner or group):
                uid = pwd.getpwnam(owner).pw_uid if owner else -1
                gid = grp.getgrnam(group).gr_gid if group else -1
                st = path.stat()
                if (owner and st.st_uid != uid) or (group and st.st_gid != gid):
                    os.chown(path, uid, gid)
        except (OSError, KeyError) as exc:
            self._add("warn", f"cannot enforce permissions on {path}: {exc}", path=str(path))

    def run(self) -> tuple[bool, list[dict[str, Any]]]:
        self._add("info", f"Security baseline starting (Eta OS {__version__})")
        log_dir = self.settings.log_dir
        state_dir = self.settings.state_dir
        config_dir = Path(str(self.settings.get("config_dir", "/etc/eta")))
        ensure_dir(log_dir, 0o770)
        ensure_dir(state_dir, 0o770)
        self._fix(log_dir, 0o770, group="eta-log")
        self._fix(state_dir, 0o770, group="eta-backend")
        self._fix(config_dir, 0o755)
        for path in log_dir.glob("*.log*"):
            self._fix(path, 0o660, group="eta-log")
        db_path = Path(str(self.settings.get("database.sqlite_path", "/var/lib/eta/eta.db")))
        if db_path.exists():
            self._fix(db_path, 0o660, group="eta-backend")
        for name in ("settings.json", "defaults.json", "install.json", "target-system"):
            self._fix(config_dir / name, 0o644)
        restic_key = Path(str(self.settings.get("backup.password_file")))
        if restic_key.exists():
            self._fix(restic_key, 0o600)
            if stat.S_IMODE(restic_key.stat().st_mode) & 0o077:
                self._add("critical", "restic key is readable by group/other", path=str(restic_key))
        else:
            self._add("warn", "restic key missing; backups cannot run", path=str(restic_key))
        # Master account must be a real, unlocked system account.
        try:
            user = pwd.getpwnam(self.settings.master_user)
            if user.pw_passwd in {"", "!", "*", "!!"}:
                self._add("critical", "master account has no usable password", user=self.settings.master_user)
            else:
                self._add("info", "master account exists with a hashed password", user=self.settings.master_user)
            try:
                group = grp.getgrnam("eta-admin")
                if self.settings.master_user not in group.gr_mem:
                    self._add("warn", "master account is not in the eta-admin group", user=self.settings.master_user)
            except KeyError:
                self._add("warn", "eta-admin group is missing")
        except KeyError:
            self._add("critical", "master account does not exist", user=self.settings.master_user)
        # World-writable configuration is never acceptable.
        try:
            for root, dirs, files in os.walk(config_dir):
                if "/source" in root:
                    continue
                for name in [*dirs, *files]:
                    path = Path(root) / name
                    try:
                        mode = stat.S_IMODE(path.lstat().st_mode)
                    except OSError:
                        continue
                    if mode & 0o002:
                        self._add("critical", f"world-writable path in configuration: {path}", path=str(path))
        except OSError:
            pass
        firewall = self.systemd.show("firewall.service")
        nftables = self.systemd.show("nftables.service")
        if firewall.active or nftables.active:
            self._add("info", "packet filter is active")
        else:
            self._add("warn", "firewall unit is not active; verify networking.firewall")
        bind = self.settings.backend_bind
        if bind not in {"127.0.0.1", "::1"}:
            self._add("warn", "backend is bound to a non-loopback address", bind=bind)
        if self.settings.enabled("ssh"):
            sshd = self.systemd.show("sshd.service")
            if not sshd.active:
                self._add("warn", "SSH is configured but sshd is not active")
        else:
            sshd = self.systemd.show("sshd.service")
            if sshd.active:
                self._add("warn", "sshd is active although SSH is disabled in settings")
        critical = [issue for issue in self.issues if issue["level"] == "critical"]
        self._add(
            "info" if not critical else "critical",
            f"Security baseline complete: {len(critical)} critical issue(s)",
            critical=len(critical),
            warnings=len([issue for issue in self.issues if issue["level"] == "warn"]),
        )
        return not critical, self.issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eta-security-check", description="Enforce the Eta OS security baseline")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"eta-security-check: configuration error: {exc}", file=sys.stderr)
        return 2
    checker = SecurityChecker(settings)
    ok, issues = checker.run()
    if args.json:
        import json

        print(json.dumps(issues, indent=2, sort_keys=True))
    else:
        for issue in issues:
            print(f"[{issue['level'].upper():8}] {issue['message']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
