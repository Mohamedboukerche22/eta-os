"""Minimal sd_notify implementation (no third-party dependency)."""

from __future__ import annotations

import os
import socket


def notify(state: str, *, unset_environment: bool = False) -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(address)
            sock.sendall(state.encode("utf-8"))
        finally:
            sock.close()
        if unset_environment:
            os.environ.pop("NOTIFY_SOCKET", None)
        return True
    except OSError:
        return False


def ready() -> bool:
    return notify("READY=1")


def reloading() -> bool:
    return notify("RELOADING=1")


def stopping() -> bool:
    return notify("STOPPING=1")


def watchdog() -> bool:
    return notify("WATCHDOG=1")


def status(text: str) -> bool:
    return notify(f"STATUS={text}")
