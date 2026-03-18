"""Tests for Google Chat gateway integration."""

import inspect
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from gateway.config import GatewayConfig, Platform, PlatformConfig, _apply_env_overrides


class TestGoogleChatPlatformEnum:
    def test_googlechat_enum_exists(self):
        assert Platform.GOOGLECHAT.value == "googlechat"

    def test_googlechat_in_platform_list(self):
        assert "googlechat" in [p.value for p in Platform]


class TestGoogleChatConfigLoading:
    @patch.dict(
        os.environ,
        {
            "GOOGLECHAT_SERVICE_ACCOUNT": "/tmp/fake-sa.json",
            "GOOGLECHAT_SPACES": "spaces/AAAA111, spaces/BBBB222",
            "GOOGLECHAT_HOME_CHANNEL": "spaces/AAAA111",
        },
        clear=False,
    )
    def test_apply_env_overrides_googlechat(self):
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.GOOGLECHAT in config.platforms
        gc = config.platforms[Platform.GOOGLECHAT]
        assert gc.enabled is True
        assert gc.token == "/tmp/fake-sa.json"
        assert gc.extra["spaces"] == ["spaces/AAAA111", "spaces/BBBB222"]
        assert gc.home_channel is not None
        assert gc.home_channel.chat_id == "spaces/AAAA111"

    @patch.dict(
        os.environ,
        {"GOOGLECHAT_SERVICE_ACCOUNT_FILE": "/tmp/sa-file.json"},
        clear=False,
    )
    def test_alias_service_account_file_is_supported(self):
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.GOOGLECHAT in config.platforms
        assert config.platforms[Platform.GOOGLECHAT].token == "/tmp/sa-file.json"

    @patch.dict(os.environ, {}, clear=True)
    def test_not_loaded_without_service_account(self):
        config = GatewayConfig()
        _apply_env_overrides(config)
        assert Platform.GOOGLECHAT not in config.platforms

    @patch.dict(
        os.environ,
        {
            "GOOGLECHAT_SERVICE_ACCOUNT": "/tmp/fake-sa.json",
            "GOOGLECHAT_SPACES": "spaces/AAAA111",
        },
        clear=False,
    )
    def test_connected_platforms_includes_googlechat(self):
        config = GatewayConfig()
        _apply_env_overrides(config)
        connected = config.get_connected_platforms()
        assert Platform.GOOGLECHAT in connected


class TestGoogleChatHelpers:
    def test_space_normalization_and_parsing(self):
        from gateway.platforms.google_chat import _normalize_space_name, _parse_spaces

        assert _normalize_space_name("AAAA111") == "spaces/AAAA111"
        assert _normalize_space_name("spaces/BBBB222") == "spaces/BBBB222"
        assert _parse_spaces("AAAA111, spaces/BBBB222, AAAA111") == [
            "spaces/AAAA111",
            "spaces/BBBB222",
        ]

    def test_human_sender_filter(self):
        from gateway.platforms.google_chat import _is_human_message

        assert _is_human_message({"sender": {"type": "HUMAN", "name": "users/1"}}) is True
        assert _is_human_message({"sender": {"type": "BOT", "name": "users/bot"}}) is False

    def test_check_requirements(self):
        from gateway.platforms.google_chat import check_googlechat_requirements

        assert isinstance(check_googlechat_requirements(), bool)


class TestGoogleChatAdapter:
    def test_build_message_event(self):
        from gateway.platforms.google_chat import GoogleChatAdapter

        cfg = PlatformConfig(
            enabled=True,
            token='{"client_email":"bot@example.com","private_key":"-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----\\n"}',
            extra={"spaces": ["spaces/AAAA111"]},
        )
        adapter = GoogleChatAdapter(cfg)

        message = {
            "name": "spaces/AAAA111/messages/msg-1",
            "text": "hello",
            "createTime": "2026-01-01T00:00:00Z",
            "sender": {"name": "users/1234", "displayName": "Alice", "type": "HUMAN"},
            "space": {"name": "spaces/AAAA111", "displayName": "Team Room", "spaceType": "SPACE"},
            "thread": {"name": "spaces/AAAA111/threads/thread-1"},
        }
        event = adapter._build_message_event(message, default_space="spaces/AAAA111")

        assert event is not None
        assert event.text == "hello"
        assert event.source.platform == Platform.GOOGLECHAT
        assert event.source.chat_id == "spaces/AAAA111"
        assert event.source.user_id == "users/1234"
        assert event.source.thread_id == "spaces/AAAA111/threads/thread-1"
        assert event.source.chat_type == "group"


class TestGoogleChatAuthorization:
    def test_googlechat_in_allowlist_maps(self):
        from gateway.run import GatewayRunner

        gw = GatewayRunner.__new__(GatewayRunner)
        gw.config = GatewayConfig()
        gw.pairing_store = MagicMock()
        gw.pairing_store.is_approved.return_value = False

        source = MagicMock()
        source.platform = Platform.GOOGLECHAT
        source.user_id = "users/1234"

        with patch.dict(os.environ, {}, clear=True):
            assert gw._is_user_authorized(source) is False


class TestGoogleChatIntegrationWiring:
    def test_googlechat_in_adapter_factory(self):
        import gateway.run

        source = inspect.getsource(gateway.run.GatewayRunner._create_adapter)
        assert "Platform.GOOGLECHAT" in source

    def test_googlechat_in_send_message_tool(self):
        import tools.send_message_tool as smt

        source = inspect.getsource(smt._handle_send)
        assert '"googlechat"' in source

    def test_googlechat_in_cron_platform_map(self):
        import cron.scheduler

        source = inspect.getsource(cron.scheduler)
        assert '"googlechat"' in source

    def test_googlechat_toolset_exists(self):
        from toolsets import TOOLSETS

        assert "hermes-googlechat" in TOOLSETS
        assert "hermes-googlechat" in TOOLSETS["hermes-gateway"]["includes"]

    def test_googlechat_platform_hint_exists(self):
        from agent.prompt_builder import PLATFORM_HINTS

        assert "googlechat" in PLATFORM_HINTS
        assert "google chat" in PLATFORM_HINTS["googlechat"].lower()

    def test_googlechat_in_channel_directory_session_discovery(self):
        import gateway.channel_directory

        source = inspect.getsource(gateway.channel_directory.build_channel_directory)
        assert '"googlechat"' in source

    def test_googlechat_in_gateway_setup_platforms(self):
        from hermes_cli.gateway import _PLATFORMS

        keys = [p["key"] for p in _PLATFORMS]
        assert "googlechat" in keys

    def test_env_example_has_googlechat_vars(self):
        env_path = Path(__file__).resolve().parents[2] / ".env.example"
        content = env_path.read_text(encoding="utf-8")
        assert "GOOGLECHAT_SERVICE_ACCOUNT" in content
        assert "GOOGLECHAT_SPACES" in content
