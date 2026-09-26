"""Dotted-field resolution shared by rules and MongoDB query helpers.

A path like ``lines.hcpcs`` fans out through arrays the same way MongoDB does.
"""
from __future__ import annotations

from typing import Any


def resolve(doc: dict, field_path: str) -> list[Any]:
    """Return every value ``doc`` has at ``field_path``, fanning out through lists."""
    values: list[Any] = [doc]
    for part in field_path.split("."):
        next_values: list[Any] = []
        for value in values:
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        next_values.append(item.get(part))
            elif isinstance(value, dict):
                next_values.append(value.get(part))
            else:
                next_values.append(None)
        values = next_values
    return values
