"""Utilities for parsing Beckhoff GVL definitions."""

from __future__ import annotations

import re

# Very permissive parser for common TwinCAT GVL declarations.
_GVL_VARIABLE_PATTERN = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_\.]*)\s*:\s*([^;]+);"
)


def parse_gvl_variables(gvl_content: str) -> list[dict[str, str]]:
    """Parse GVL content and return variables as name/type mappings."""
    variables: list[dict[str, str]] = []
    in_var_block = False

    for raw_line in gvl_content.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        if line.startswith("(*") or line.startswith("//"):
            continue

        if line.upper().startswith("VAR"):
            in_var_block = True
            continue

        if line.upper().startswith("END_VAR"):
            in_var_block = False
            continue

        if not in_var_block:
            continue

        # Ignore TwinCAT attributes/pragmas and region markers.
        if line.startswith("{") or line.startswith("#"):
            continue

        match = _GVL_VARIABLE_PATTERN.match(raw_line)
        if not match:
            continue

        name = match.group(1).strip()
        plc_type = match.group(2).strip()

        variables.append({"name": name, "type": plc_type})

    return variables
