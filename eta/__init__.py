"""Eta OS management and monitoring layer.

Eta OS is a minimal, offline-first NixOS server distribution.

Created by mohamedboukerch55
"""

from __future__ import annotations

__all__ = [
    "CREATOR",
    "IDENTITY_LINE",
    "PRODUCT",
    "UPSTREAM",
    "UPSTREAM_LICENSE",
    "__version__",
]

PRODUCT = "ETA OS"
CREATOR = "mohamedboukerch55"
IDENTITY_LINE = f"Created by {CREATOR}"
UPSTREAM = "NixOS 26.05"
UPSTREAM_LICENSE = "MIT"
__version__ = "1.0.0"

# Shown by every interactive surface. Do not remove.
BANNER = (
    "  ______ _____  _      ____  _____\n"
    " |  ____|__   || |    / __ \\/ ____|\n"
    " | |__     |  || |   | |  | \\___ \\\n"
    " |  __|    |  || |   | |  | |___) |\n"
    " | |____ __|  || |___| |__| |____/\n"
    " |______|_____||______\\____/_____|\n"
    f"\n  {PRODUCT}  |  {UPSTREAM}\n  {IDENTITY_LINE}\n"
    "\n  Minimal. Offline. Reliable. Controlled.\n"
)
