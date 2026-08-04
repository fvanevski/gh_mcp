"""Settings validation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_gh_server.settings import Settings


class TestSettingsValidation:
    """Test cross-field validators in Settings."""

    def test_default_max_exceeds_hard_raises(self) -> None:
        with pytest.raises(ValidationError, match="DEFAULT_MAX_RESULTS"):
            Settings(
                default_max_results=200,
                hard_max_results=50,
            )

    def test_valid_limits(self) -> None:
        settings = Settings(
            default_max_results=30,
            hard_max_results=100,
        )
        assert settings.default_max_results == 30
        assert settings.hard_max_results == 100

    def test_default_values(self) -> None:
        settings = Settings(
            _env_file=None,
            allow_write_commands=False,
            confirm_write_commands=True,
            transport="stdio",
            log_level="INFO",
            http_port=8766,
        )
        assert settings.allow_write_commands is False
        assert settings.confirm_write_commands is True
        assert settings.transport == "stdio"
        assert settings.log_level == "INFO"
        assert settings.http_port == 8766
