"""Size-based rotation for /var/log/eta (journald has its own limits)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_settings
from .logger import EtaLogger, rotate_file
from .util import human_bytes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eta-log-rotate", description="Rotate Eta OS persistent logs")
    parser.add_argument("--dir", default=None)
    parser.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--keep", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except Exception:
        settings = None
    directory = Path(args.dir or (settings.log_dir if settings else "/var/log/eta"))
    logger = EtaLogger("services", directory, run_dir=(settings.run_dir if settings else "/run/eta"))
    rotated = []
    try:
        paths = sorted(directory.glob("*.log"))
    except OSError as exc:
        print(f"eta-log-rotate: {exc}", file=sys.stderr)
        return 1
    for path in paths:
        if rotate_file(path, max_bytes=args.max_bytes, backups=args.keep):
            rotated.append(path.name)
    if rotated:
        logger.info(
            "Log rotation complete",
            event="log_rotate",
            files=rotated,
            max_bytes=args.max_bytes,
            keep=args.keep,
        )
        print("rotated: " + ", ".join(rotated))
    else:
        print("nothing to rotate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
