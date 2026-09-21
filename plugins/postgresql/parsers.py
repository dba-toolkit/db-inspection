"""Low-level parsers for PostgreSQL collector evidence.

SAR history parsing lives in ``inspection_core`` — the series come from
``charts.history`` and the coverage estimate from ``sampling``.  This module only
keeps the parsers that read PostgreSQL-specific collector files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspection_core.values import safe_float


def _parse_df_pt(path: Path) -> list[dict[str, Any]]:
    """Parse df -PT output (space-separated with multi-word mountpoints)."""
    result: list[dict[str, Any]] = []
    if not path.exists():
        return result
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) < 2:
        return result
    # Skip header, parse each line from the end (mountpoint may contain spaces)
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        # Last field is mountpoint; second-to-last is capacity (NN%)
        cap_str = parts[-2].rstrip("%")
        mp = parts[-1]
        result.append({
            "filesystem": parts[0],
            "fstype": parts[1],
            "blocks": safe_float(parts[2]),
            "used": safe_float(parts[3]),
            "available": safe_float(parts[4]),
            "usage_percent": safe_float(cap_str),
            "mountpoint": mp,
        })
    return result


def _parse_free_b(path: Path) -> dict[str, Any] | None:
    """Parse free -b output, returning Mem: row with bytes values."""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 7:
                return {
                    "total": safe_float(parts[1]),
                    "used": safe_float(parts[2]),
                    "free": safe_float(parts[3]),
                    "available": safe_float(parts[6]),
                }
    return None
