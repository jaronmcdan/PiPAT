"""ROI (Remote Operational Equipment)

A Raspberry Pi–focused CAN-to-instrument bridge for lab / test automation.

Entry point: `roi` (console script) or `python -m roi`.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("roi")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"
