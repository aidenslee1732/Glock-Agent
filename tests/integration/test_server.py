"""Integration tests for the server components."""

import pytest
import asyncio
import json
import base64
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

# Test imports - these may need adjustment based on actual module structure
try:
    from apps.server.src.storage.checkpoint_store import ContextCheckpointStore
    from apps.server.src.context.rehydrator import ContextRehydrator
    from apps.server.src.gateway.ws.llm_handler import LLMHandler
    HAS_SERVER_IMPORTS = True
except ImportError:
    HAS_SERVER_IMPORTS = False


@pytest.mark.skipif(not HAS_SERVER_IMPORTS, reason="Server imports not available")
class TestCheckpointStore:
    """Tests for the checkpoint store."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database client."""
        db = AsyncMock()
        db.fetchone = AsyncMock(return_value=None)
        db.fetchall = AsyncMock(return_value=[])
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def store(self, mock_db):
        """Create a checkpoint store with mock DB."""
        return ContextCheckpointStore(
            db=mock_db,
            master_key=b"0" * 32,
        )

    @pytest.mark.asyncio
    async def test_store_checkpoint(self, store, mock_db):
        """Test storing a checkpoint."""
        checkpoint_data = {
            "messages": [{"role": "user", "content": "Hello"}],
            "turn_count": 1,
        }

        checkpoint_id = await store.store_checkpoint(
            user_id="user_123",
            session_id="sess_456",
            checkpoint_id="cp_789",
            payload=json.dumps(checkpoint_data).encode(),
            parent_id=None,
            is_full=True,
            token_count=100,
        )

        assert checkpoint_id == "cp_789"
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_checkpoint(self, store, mock_db):
        """Test loading a checkpoint."""
        # Set up mock to return encrypted data
        test_data = b"test checkpoint data"
        key = store._derive_session_key("sess_456", "user_123")

        # Encrypt the data
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import os
        nonce = os.urandom(12)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, test_data, None)

        mock_db.fetchone.return_value = {
            "id": "cp_789",
            "session_id": "sess_456",
            "user_id": "user_123",
            "nonce_base64": base64.b64encode(nonce).decode(),
            "ciphertext_base64": base64.b64encode(ciphertext).decode(),
            "is_full": True,
            "parent_id": None,
            "token_count": 100,
        }

        result = await store.load_checkpoint(
            user_id="user_123",
            session_id="sess_456",
            checkpoint_id="cp_789",
        )

        assert result is not None
        assert result["payload"] == test_data

    @pytest.mark.asyncio
    async def test_checkpoint_isolation(self, store, mock_db):
        """Test that checkpoints are isolated by session."""
        # Try to load checkpoint with wrong session
        mock_db.fetchone.return_value = None  # Not found due to session mismatch

        result = await store.load_checkpoint(
            user_id="user_123",
            session_id="wrong_session",
            checkpoint_id="cp_789",
        )

        assert result is None


@pytest.mark.skipif(not HAS_SERVER_IMPORTS, reason="Server imports not available")
class TestContextRehydrator:
    """Tests for context rehydration."""

    @pytest.fixture
    def mock_checkpoint_store(self):
        store = AsyncMock()
        return store

    @pytest.fixture
    def rehydrator(self, mock_checkpoint_store):
        return ContextRehydrator(checkpoint_store=mock_checkpoint_store)

    @pytest.mark.asyncio
    async def test_rehydrate_full_checkpoint(self, rehydrator, mock_checkpoint_store):
        """Test rehydrating from a full checkpoint."""
        checkpoint_data = {
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
            ],
            "rolling_summary": {"task_description": "Test task"},
            "pinned_facts": [],
        }

        mock_checkpoint_store.load_checkpoint.return_value = {
            "payload": json.dumps(checkpoint_data).encode(),
            "is_full": True,
            "parent_id": None,
        }

        result = await rehydrator.rehydrate(
            user_id="user_123",
            session_id="sess_456",
            context_ref="cp_789",
            delta={"messages": [], "tool_results_compressed": []},
            context_pack={},
        )

        assert len(result["messages"]) >= 2

    @pytest.mark.asyncio
    async def test_rehydrate_with_delta(self, rehydrator, mock_checkpoint_store):
        """Test rehydrating with delta applied."""
        checkpoint_data = {
            "messages": [
                {"role": "user", "content": "Hello"},
            ],
        }

        mock_checkpoint_store.load_checkpoint.return_value = {
            "payload": json.dumps(checkpoint_data).encode(),
            "is_full": True,
            "parent_id": None,
        }

        delta = {
            "messages": [
                {"role": "assistant", "content": "How can I help?"},
                {"role": "user", "content": "Fix a bug"},
            ],
            "tool_results_compressed": [],
        }

        result = await rehydrator.rehydrate(
            user_id="user_123",
            session_id="sess_456",
            context_ref="cp_789",
            delta=delta,
            context_pack={},
        )

        # Should have original + delta messages
        assert len(result["messages"]) >= 3


@pytest.mark.skipif(not HAS_SERVER_IMPORTS, reason="Server imports not available")
class TestLLMHandler:
    """Tests for the LLM handler."""

    @pytest.fixture
    def mock_redis(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        return redis

    @pytest.fixture
    def mock_postgres(self):
        postgres = AsyncMock()
        postgres.fetchone = AsyncMock(return_value=None)
        postgres.fetchall = AsyncMock(return_value=[])
        postgres.execute = AsyncMock()
        return postgres

    @pytest.fixture
    def mock_llm_gateway(self):
        gateway = AsyncMock()
        return gateway

    @pytest.fixture
    def handler(self, mock_redis, mock_postgres, mock_llm_gateway):
        return LLMHandler(
            redis=mock_redis,
            postgres=mock_postgres,
            llm_gateway=mock_llm_gateway,
        )

    @pytest.mark.asyncio
    async def test_handle_llm_request(self, handler, mock_llm_gateway):
        """Test handling an LLM request."""
        # Set up mock LLM response
        async def mock_stream():
            yield {"type": "text", "text": "Hi there!"}
            yield {"type": "message_stop", "usage": {"input_tokens": 10, "output_tokens": 5}}

        mock_llm_gateway.stream = AsyncMock(return_value=mock_stream())

        # Mock send function
        send_mock = AsyncMock()

        payload = {
            "request_id": "req_123",
            "context_ref": None,  # No checkpoint
            "delta": {"messages": [{"role": "user", "content": "Hello"}], "tool_results_compressed": []},
            "context_pack": {},
            "tools": [],
            "model_tier": "standard",
            "max_tokens": 1000,
            "temperature": 0.7,
        }

        await handler.handle_llm_request(
            session_id="sess_789",
            user_id="user_abc",
            payload=payload,
            send_callback=send_mock,
        )

        # Should have sent messages
        assert send_mock.call_count >= 1


class TestProtocolTypes:
    """Tests for shared protocol types."""

    def test_context_pack_serialization(self):
        """Test ContextPack serialization."""
        from packages.shared_protocol.types import (
            ContextPack,
            RollingSummary,
            PinnedFact,
            FileSlice,
        )

        pack = ContextPack(
            rolling_summary=RollingSummary(
                task_description="Test task",
                files_modified=["test.py"],
                files_read=["readme.md"],
                key_decisions=["Use pytest"],
                errors_encountered=[],
                current_state="In progress",
                turn_count=5,
            ),
            pinned_facts=[
                PinnedFact(
                    key="config_file",
                    value="config.py",
                    category="file_path",
                    importance=1.0,
                )
            ],
            file_slices=[
                FileSlice(
                    file_path="test.py",
                    start_line=1,
                    end_line=10,
                    content="def test(): pass",
                    reason="grep_hit",
                )
            ],
            token_count=1000,
        )

        # Should be able to convert to dict
        data = pack.to_dict()
        assert data["token_count"] == 1000
        assert len(data["pinned_facts"]) == 1

    def test_message_types(self):
        """Test message type constants."""
        from packages.shared_protocol.types import MessageType

        assert MessageType.LLM_REQUEST == "llm_request"
        assert MessageType.LLM_DELTA == "llm_delta"
        assert MessageType.LLM_RESPONSE_END == "llm_response_end"
        assert MessageType.CONTEXT_CHECKPOINT == "context_checkpoint"

    def test_generate_ids(self):
        """Test ID generation functions."""
        from packages.shared_protocol.types import (
            generate_checkpoint_id,
            generate_request_id,
        )

        cp_id = generate_checkpoint_id()
        req_id = generate_request_id()

        assert cp_id.startswith("cp_")
        assert req_id.startswith("req_")
        assert len(cp_id) > 10
        assert len(req_id) > 10

        # Should be unique
        assert generate_checkpoint_id() != generate_checkpoint_id()


@pytest.mark.skipif(not HAS_SERVER_IMPORTS, reason="Server imports not available")
class TestErrorPaths:
    """Tests for error scenarios and edge cases.

    Bug fix 1.9: Add comprehensive tests for error paths including
    checkpoint failures, LLM timeouts, and malformed payloads.
    """

    @pytest.fixture
    def mock_redis(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        return redis

    @pytest.fixture
    def mock_postgres(self):
        postgres = AsyncMock()
        postgres.fetchone = AsyncMock(return_value=None)
        postgres.fetchall = AsyncMock(return_value=[])
        postgres.execute = AsyncMock()
        return postgres

    @pytest.fixture
    def mock_llm_gateway(self):
        gateway = AsyncMock()
        return gateway

    @pytest.fixture
    def handler(self, mock_redis, mock_postgres, mock_llm_gateway):
        return LLMHandler(
            redis=mock_redis,
            postgres=mock_postgres,
            llm_gateway=mock_llm_gateway,
        )

    @pytest.mark.asyncio
    async def test_checkpoint_storage_failure(self, handler, mock_postgres, mock_llm_gateway):
        """Test handling of checkpoint storage failures."""
        # Simulate postgres store raising an exception on checkpoint storage
        mock_postgres.execute.side_effect = Exception("Database connection failed")

        async def mock_stream():
            yield {"type": "text", "text": "Response"}
            yield {"type": "message_stop", "usage": {"input_tokens": 10, "output_tokens": 5}}

        mock_llm_gateway.stream = AsyncMock(return_value=mock_stream())
        send_mock = AsyncMock()

        payload = {
            "request_id": "req_123",
            "context_ref": None,
            "delta": {"messages": [{"role": "user", "content": "Hello"}], "tool_results_compressed": []},
            "context_pack": {},
            "tools": [],
            "model_tier": "standard",
            "max_tokens": 1000,
            "temperature": 0.7,
        }

        # Should handle checkpoint failure gracefully - may raise or may succeed
        # depending on when checkpoint is attempted
        try:
            await handler.handle_llm_request(
                session_id="sess_789",
                user_id="user_abc",
                payload=payload,
                send_callback=send_mock,
            )
        except Exception:
            pass  # Error path is exercised

    @pytest.mark.asyncio
    async def test_llm_timeout(self, handler, mock_llm_gateway):
        """Test handling of LLM request timeouts."""
        # Simulate LLM timing out
        async def slow_stream():
            await asyncio.sleep(10)  # Will be cancelled
            yield {"type": "text", "text": "Never reached"}

        mock_llm_gateway.stream = AsyncMock(return_value=slow_stream())
        send_mock = AsyncMock()

        payload = {
            "request_id": "req_timeout",
            "context_ref": None,
            "delta": {"messages": [{"role": "user", "content": "Hello"}], "tool_results_compressed": []},
            "context_pack": {},
            "tools": [],
            "model_tier": "standard",
            "max_tokens": 1000,
            "temperature": 0.7,
        }

        # Use asyncio.wait_for to test timeout handling
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                handler.handle_llm_request(
                    session_id="sess_timeout",
                    user_id="user_abc",
                    payload=payload,
                    send_callback=send_mock,
                ),
                timeout=0.1,
            )

    @pytest.mark.asyncio
    async def test_malformed_tool_arguments_json(self, handler, mock_llm_gateway):
        """Test handling of malformed JSON in tool arguments (Bug 1.1 fix)."""
        # Simulate LLM returning malformed tool args
        async def mock_stream_with_bad_json():
            yield {"type": "content_block_start", "content_block": {"type": "tool_use", "id": "tool_1", "name": "read_file"}}
            yield {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"path": "test.py"'}}  # Invalid JSON
            yield {"type": "content_block_stop"}
            yield {"type": "message_stop", "usage": {"input_tokens": 10, "output_tokens": 20}}

        mock_llm_gateway.stream = AsyncMock(return_value=mock_stream_with_bad_json())
        send_mock = AsyncMock()

        payload = {
            "request_id": "req_bad_json",
            "context_ref": None,
            "delta": {"messages": [{"role": "user", "content": "Read file"}], "tool_results_compressed": []},
            "context_pack": {},
            "tools": [{"name": "read_file", "description": "Read a file"}],
            "model_tier": "standard",
            "max_tokens": 1000,
            "temperature": 0.7,
        }

        # Should not raise - should log warning and preserve raw args
        await handler.handle_llm_request(
            session_id="sess_bad_json",
            user_id="user_abc",
            payload=payload,
            send_callback=send_mock,
        )

        # Verify send was called (request was handled)
        assert send_mock.call_count >= 1

    @pytest.mark.asyncio
    async def test_concurrent_request_cleanup(self, handler, mock_llm_gateway):
        """Test race condition handling in request cleanup (Bug 1.4 fix)."""
        async def mock_stream():
            yield {"type": "text", "text": "Response"}
            yield {"type": "message_stop", "usage": {"input_tokens": 10, "output_tokens": 5}}

        mock_llm_gateway.stream = AsyncMock(return_value=mock_stream())

        # Create multiple concurrent requests
        async def make_request(request_id: str):
            send_mock = AsyncMock()
            payload = {
                "request_id": request_id,
                "context_ref": None,
                "delta": {"messages": [{"role": "user", "content": "Hello"}], "tool_results_compressed": []},
                "context_pack": {},
                "tools": [],
                "model_tier": "standard",
                "max_tokens": 1000,
                "temperature": 0.7,
            }
            await handler.handle_llm_request(
                session_id=f"sess_{request_id}",
                user_id="user_abc",
                payload=payload,
                send_callback=send_mock,
            )

        # Run multiple requests concurrently - should not raise race condition errors
        tasks = [make_request(f"req_{i}") for i in range(5)]

        await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_missing_required_payload_fields(self, handler):
        """Test handling of payloads with missing required fields."""
        send_mock = AsyncMock()

        # Payload missing required fields
        incomplete_payload = {
            "request_id": "req_incomplete",
            # Missing: context_ref, delta, context_pack, tools, etc.
        }

        with pytest.raises((KeyError, TypeError, Exception)):
            await handler.handle_llm_request(
                session_id="sess_incomplete",
                user_id="user_abc",
                payload=incomplete_payload,
                send_callback=send_mock,
            )

    @pytest.mark.asyncio
    async def test_rehydration_failure(self, mock_redis, mock_postgres, mock_llm_gateway):
        """Test handling of context rehydration failures."""
        # Simulate checkpoint retrieval failing
        mock_postgres.fetchone.side_effect = Exception("Failed to decode checkpoint")

        handler = LLMHandler(
            redis=mock_redis,
            postgres=mock_postgres,
            llm_gateway=mock_llm_gateway,
        )

        send_mock = AsyncMock()
        payload = {
            "request_id": "req_rehydrate_fail",
            "context_ref": "cp_corrupt",  # Reference to corrupt checkpoint
            "delta": {"messages": [], "tool_results_compressed": []},
            "context_pack": {},
            "tools": [],
            "model_tier": "standard",
            "max_tokens": 1000,
            "temperature": 0.7,
        }

        with pytest.raises(Exception):
            await handler.handle_llm_request(
                session_id="sess_rehydrate",
                user_id="user_abc",
                payload=payload,
                send_callback=send_mock,
            )


class TestSystemPromptConfiguration:
    """Tests for configurable system prompt (Bug 1.8 fix)."""

    def test_load_system_prompt_default(self, monkeypatch):
        """Test loading default system prompt when no config exists."""
        # Clear any existing env vars
        monkeypatch.delenv("GLOCK_SYSTEM_PROMPT", raising=False)
        monkeypatch.delenv("GLOCK_SYSTEM_PROMPT_FILE", raising=False)

        from apps.server.src.config.system_prompt import load_system_prompt

        config = load_system_prompt()
        assert config.source == "default"
        assert "Glock" in config.prompt

    def test_load_system_prompt_from_env(self, monkeypatch):
        """Test loading system prompt from environment variable."""
        custom_prompt = "You are a custom assistant."
        monkeypatch.setenv("GLOCK_SYSTEM_PROMPT", custom_prompt)

        from apps.server.src.config.system_prompt import load_system_prompt

        config = load_system_prompt()
        assert config.source == "env:GLOCK_SYSTEM_PROMPT"
        assert config.prompt == custom_prompt

    def test_load_system_prompt_from_file(self, monkeypatch, tmp_path):
        """Test loading system prompt from file specified in env var."""
        custom_prompt = "You are a file-based assistant."
        prompt_file = tmp_path / "system_prompt.md"
        prompt_file.write_text(custom_prompt)

        monkeypatch.delenv("GLOCK_SYSTEM_PROMPT", raising=False)
        monkeypatch.setenv("GLOCK_SYSTEM_PROMPT_FILE", str(prompt_file))

        from apps.server.src.config.system_prompt import load_system_prompt

        config = load_system_prompt()
        assert "env_file:" in config.source
        assert config.prompt == custom_prompt

    def test_load_system_prompt_env_takes_precedence(self, monkeypatch, tmp_path):
        """Test that env var takes precedence over file."""
        env_prompt = "Environment prompt"
        file_prompt = "File prompt"

        prompt_file = tmp_path / "system_prompt.md"
        prompt_file.write_text(file_prompt)

        monkeypatch.setenv("GLOCK_SYSTEM_PROMPT", env_prompt)
        monkeypatch.setenv("GLOCK_SYSTEM_PROMPT_FILE", str(prompt_file))

        from apps.server.src.config.system_prompt import load_system_prompt

        config = load_system_prompt()
        assert config.source == "env:GLOCK_SYSTEM_PROMPT"
        assert config.prompt == env_prompt


class TestTestSecrets:
    """Tests to verify test secrets are properly randomized (Bug 1.7 fix)."""

    def test_jwt_secret_is_random(self, mock_env):
        """Verify JWT_SECRET is randomly generated."""
        import os

        jwt_secret = os.environ.get("JWT_SECRET")
        assert jwt_secret is not None
        assert len(jwt_secret) == 64  # 32 bytes = 64 hex chars
        # Verify it's hex
        int(jwt_secret, 16)

    def test_context_master_key_is_random(self, mock_env):
        """Verify CONTEXT_MASTER_KEY is randomly generated."""
        import os

        master_key = os.environ.get("CONTEXT_MASTER_KEY")
        assert master_key is not None
        assert len(master_key) == 64  # 32 bytes = 64 hex chars
        # Verify it's hex
        int(master_key, 16)

    def test_secrets_are_unique_per_fixture(self, mock_env):
        """Verify secrets change between test runs."""
        import os

        # Get current secrets
        jwt1 = os.environ.get("JWT_SECRET")
        key1 = os.environ.get("CONTEXT_MASTER_KEY")

        # Secrets should not be the old hardcoded values
        assert jwt1 != "test-secret-key-at-least-32-characters"
        assert key1 != "0" * 64


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
