"""Optional AI assistant for the water gateway.

Everything in this package is imported lazily by :mod:`gateway.main` and only
when ``AI_ENABLED=true``. Nothing here is required for the Modbus/BACnet
pipeline to run.
"""

from __future__ import annotations

__all__ = ["bedrock", "tools", "agent"]
