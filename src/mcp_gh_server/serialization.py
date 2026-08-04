"""Conversion of gh CLI output values to JSON-safe MCP structured output."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import JsonValue


def to_json_value(value: Any) -> JsonValue:
    """Normalize common values from gh CLI output without losing precision."""

    if value is None or isinstance(value, (str, bool, int)):
        return value

    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, (datetime, date, time)):
        return value.isoformat()

    if isinstance(value, timedelta):
        return str(value)

    if isinstance(value, (UUID, Path, Enum)):
        return str(value)

    if isinstance(value, bytes):
        import base64

        encoded = base64.b64encode(value).decode("ascii")
        return f"base64:{encoded}"

    if isinstance(value, Mapping):
        return {str(key): to_json_value(item) for key, item in value.items()}

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_json_value(item) for item in value]

    return str(value)
