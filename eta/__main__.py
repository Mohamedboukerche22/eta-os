"""``python -m eta`` dispatcher (development convenience)."""

from __future__ import annotations

import importlib
import sys

MODULES = {
    "admin": "eta.admin",
    "boot": "eta.boot",
    "monitor": "eta.monitor",
    "backup": "eta.backup",
    "health": "eta.health",
    "maintenance": "eta.maintenance",
    "install": "eta.install",
    "priv": "eta.priv",
    "backend": "eta.backend",
    "database": "eta.database",
    "settings": "eta.settings_apply",
    "security": "eta.security_check",
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print("usage: python -m eta <" + "|".join(sorted(MODULES)) + "> [args...]")
        return 0 if len(sys.argv) >= 2 else 2
    name = sys.argv[1]
    if name not in MODULES:
        print(f"unknown module: {name}", file=sys.stderr)
        return 2
    module = importlib.import_module(MODULES[name])
    sys.argv = [f"eta-{name}", *sys.argv[2:]]
    return int(module.main())


if __name__ == "__main__":
    raise SystemExit(main())
