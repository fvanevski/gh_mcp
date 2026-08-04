"""JSON-safe serialization tests."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from mcp_gh_server.serialization import to_json_value


class TestToJsonValue:
    """Test that common Python values are normalized to JSON-safe types."""

    def test_none(self) -> None:
        assert to_json_value(None) is None

    def test_str(self) -> None:
        assert to_json_value("hello") == "hello"

    def test_bool(self) -> None:
        assert to_json_value(True) is True
        assert to_json_value(False) is False

    def test_int(self) -> None:
        assert to_json_value(42) == 42

    def test_finite_float(self) -> None:
        assert to_json_value(3.14) == 3.14

    def test_infinite_float(self) -> None:
        assert to_json_value(float("inf")) == "inf"
        assert to_json_value(float("-inf")) == "-inf"

    def test_decimal(self) -> None:
        assert to_json_value(Decimal("123.456")) == "123.456"

    def test_datetime(self) -> None:
        dt = datetime(2026, 1, 15, 10, 30, 0)
        assert to_json_value(dt) == "2026-01-15T10:30:00"

    def test_date(self) -> None:
        d = date(2026, 1, 15)
        assert to_json_value(d) == "2026-01-15"

    def test_time(self) -> None:
        t = time(10, 30, 0)
        assert to_json_value(t) == "10:30:00"

    def test_timedelta(self) -> None:
        td = timedelta(hours=1, minutes=30)
        assert to_json_value(td) == "1:30:00"

    def test_uuid(self) -> None:
        uid = UUID("12345678-1234-5678-1234-567812345678")
        assert to_json_value(uid) == "12345678-1234-5678-1234-567812345678"

    def test_path(self) -> None:
        assert to_json_value(Path("/tmp/test")) == "/tmp/test"

    def test_bytes(self) -> None:
        assert to_json_value(b"hello") == "base64:aGVsbG8="

    def test_mapping(self) -> None:
        data = {"name": "test", "count": 42}
        result = to_json_value(data)
        assert result == {"name": "test", "count": 42}

    def test_nested_mapping(self) -> None:
        data = {"meta": {"created": "2026-01-01"}}
        result = to_json_value(data)
        assert result == {"meta": {"created": "2026-01-01"}}

    def test_list(self) -> None:
        assert to_json_value([1, "two", True]) == [1, "two", True]

    def test_string_in_list(self) -> None:
        assert to_json_value(["a", "b"]) == ["a", "b"]

    def test_bytes_in_list(self) -> None:
        result = to_json_value([b"hello"])
        assert result == ["base64:aGVsbG8="]
