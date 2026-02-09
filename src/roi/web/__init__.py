"""Web dashboard for ROI.

The web UI is intentionally dependency-free (stdlib only) so it can run on
minimal Raspberry Pi images and in constrained environments.

See :mod:`roi.web.server`.
"""

from __future__ import annotations

from .server import WebDashboardServer, WebServerConfig

__all__ = ["WebDashboardServer", "WebServerConfig"]
