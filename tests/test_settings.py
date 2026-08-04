"""Settings validation tests."""

from __future__ import annotations

from pathlib import Path

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
            transport="stdio",
            log_level="INFO",
            http_port=8766,
        )
        assert settings.allow_write_commands is False
        assert settings.allow_repo_creation is False
        assert settings.allow_release_creation is False
        assert settings.allow_workflow_dispatch is False
        assert settings.allow_content_commits is False
        assert settings.max_commit_files == 100
        assert settings.max_file_bytes == 1_000_000
        assert settings.max_commit_bytes == 5_000_000
        assert settings.max_pr_diff_bytes == 500_000
        assert settings.max_pr_file_patch_bytes == 8_000
        assert settings.max_pr_commit_message_bytes == 4_000
        assert settings.transport == "stdio"
        assert settings.log_level == "INFO"
        assert settings.http_port == 8766

    def test_unprefixed_github_token_loads_from_dotenv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("MCP_GH_GITHUB_TOKEN", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("GITHUB_TOKEN=from-dotenv\n")

        settings = Settings(_env_file=env_file)

        assert settings.github_token is not None
        assert settings.github_token.get_secret_value() == "from-dotenv"
