"""PFLM exposure-alignment prep package (design PC).

Reads a wafer GDS of pinfin/heater arrays and produces a *set folder* — the
on-disk contract (§4 of docs/ARCHITECTURE.md) consumed by the offline laser PC:
``plan.json`` (exposure schedule), one centered ``jobs/<array_id>.dxf`` per
array, ``manifest.csv``, and ``prep_log.txt``.

This half runs on the design PC (Python 3.11+) and uses ``klayout.db`` + ``ezdxf``.
It NEVER imports hardware libraries (pyserial / pywin32 / tkinter); the only thing
crossing to the laser PC is the set folder.
"""

from __future__ import annotations

__version__ = "1.0.0"
schema_version = 1

__all__ = ["__version__", "schema_version"]
