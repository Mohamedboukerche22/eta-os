"""Apply mutable installer settings at boot.

The primary installation path is fully declarative. This service exists for the
baked-closure fallback and for hostname changes made by the installer, so the
runtime name always matches ``/etc/eta/settings.json`` even if the Nix store
hostname is the default.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path
from typing import Callable

from . import __version__
from .config import Settings, load_settings
from .logger import EtaLogger
from .util import atomic_write_json, check_hostname, utc_iso


def apply_settings(settings: Settings, *, logger: EtaLogger | None = None, clock: Callable[[], float] = None) -> dict:
    import time

    clock = clock or time.time
    logger = logger or EtaLogger("services", settings.log_dir, run_dir=settings.run_dir, clock=clock)
    desired = str(settings.get("hostname", "eta"))
    try:
        check_hostname(desired)
    except Exception as exc:
        logger.error(f"invalid configured hostname: {exc}", event="settings_hostname_invalid")
        raise
    current = socket.gethostname()
    changed = current != desired
    if changed:
        try:
            socket.sethostname(desired)
            logger.info(
                "Hostname applied from Eta settings",
                event="settings_hostname_applied",
                previous=current,
                hostname=desired,
            )
        except OSError as exc:
            logger.error(f"cannot apply hostname: {exc}", event="settings_hostname_failed")
            raise
    payload = {
        "schema": "eta.runtime.v1",
        "applied_at": utc_iso(clock()),
        "eta_version": __version__,
        "hostname": desired,
        "previous_hostname": current,
        "changed": changed,
    }
    try:
        atomic_write_json(settings.state_dir / "runtime.json", payload, mode=0o644)
    except OSError:
        pass
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eta-settings-apply", description="Apply Eta OS runtime settings")
    parser.parse_args(argv)
    try:
        settings = load_settings()
        apply_settings(settings)
    except Exception as exc:
        print(f"eta-settings-apply: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
