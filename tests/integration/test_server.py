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
    def mock_deps(self):
        return {
            "checkpoint_store": AsyncMock(),
            "rehydrator": AsyncMock(),
            "metering": AsyncMock(),
            "llm_client": AsyncMock(),
        }

    @pytest.fixture
    def handler(self, mock_deps):
        return LLMHandler(**mock_deps)

    @pytest.mark.asyncio
    async def test_handle_llm_request(self, handler, mock_deps):
        """Test handling an LLM request."""
        # Set up mock rehydrator
        mock_deps["rehydrator"].rehydrate.return_value = {
            "messages": [
                {"role": "user", "content": "Hello"},
            ],
            "system_prompt": "You are a helpful assistant.",
        }

        # Set up mock LLM response
        async def mock_stream():
            yield {"type": "text", "content": "Hi "}
            yield {"type": "text", "content": "there!"}
            yield {"type": "end", "usage": {"input_tokens": 10, "output_tokens": 5}}

        mock_deps["llm_client"].stream.return_value = mock_stream()

        # Mock send function
        send_mock = AsyncMock()

        payload = {
            "request_id": "req_123",
            "context_ref": "cp_456",
            "delta": {"messages": [], "tool_results_compressed": []},
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
            send=send_mock,
        )

        # Should have sent delta messages
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
