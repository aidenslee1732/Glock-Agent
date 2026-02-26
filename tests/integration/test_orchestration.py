"""Integration tests for the Orchestration Engine."""

import pytest
import asyncio
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from apps.cli.src.orchestrator.engine import (
    OrchestrationEngine,
    OrchestrationConfig,
    OrchestrationEvent,
    EventType,
)
from apps.cli.src.context.packer import ContextPacker
from apps.cli.src.crypto.session_keys import SessionKeyManager


class MockWebSocketClient:
    """Mock WebSocket client for testing."""

    def __init__(self):
        self._llm_delta_handler = None
        self._llm_response_handler = None
        self._llm_error_handler = None
        self._checkpoint_ack_handler = None

        self.sent_messages = []
        self.llm_responses = []
        self._response_index = 0

    def on_llm_delta(self, handler):
        self._llm_delta_handler = handler

    def on_llm_response(self, handler):
        self._llm_response_handler = handler

    def on_llm_error(self, handler):
        self._llm_error_handler = handler

    def on_checkpoint_ack(self, handler):
        self._checkpoint_ack_handler = handler

    async def send_llm_request(self, **kwargs):
        self.sent_messages.append(("llm_request", kwargs))

        # Simulate response after a short delay
        await asyncio.sleep(0.01)

        if self._response_index < len(self.llm_responses):
            response = self.llm_responses[self._response_index]
            self._response_index += 1

            # Trigger delta handler
            if self._llm_delta_handler and "content" in response:
                from packages.shared_protocol.types import LLMDeltaPayload
                delta = LLMDeltaPayload(
                    request_id=kwargs["request_id"],
                    delta_type="text",
                    content=response["content"],
                    token_count=len(response["content"]) // 4,
                )
                self._llm_delta_handler(delta)

            # Trigger response end handler
            if self._llm_response_handler:
                from packages.shared_protocol.types import LLMResponseEndPayload, ToolCallResult
                tool_calls = None
                if "tool_calls" in response:
                    tool_calls = [
                        ToolCallResult(
                            tool_call_id=tc["id"],
                            tool_name=tc["name"],
                            args=tc["args"],
                        )
                        for tc in response["tool_calls"]
                    ]

                end_payload = LLMResponseEndPayload(
                    request_id=kwargs["request_id"],
                    new_context_ref=f"cp_{self._response_index}",
                    finish_reason="stop" if not tool_calls else "tool_use",
                    total_input_tokens=100,
                    total_output_tokens=50,
                    tool_call_results=tool_calls,
                )
                self._llm_response_handler(end_payload)

    async def send_llm_cancel(self, **kwargs):
        self.sent_messages.append(("llm_cancel", kwargs))

    async def send_context_checkpoint(self, **kwargs):
        self.sent_messages.append(("checkpoint", kwargs))

        # Simulate ACK
        if self._checkpoint_ack_handler:
            from packages.shared_protocol.types import ContextCheckpointAckPayload
            ack = ContextCheckpointAckPayload(
                checkpoint_id=kwargs["checkpoint_id"],
                stored=True,
                expires_at="2025-01-01T00:00:00Z",
            )
            self._checkpoint_ack_handler(ack)


class MockToolBroker:
    """Mock tool broker for testing."""

    def __init__(self):
        self.executed_tools = []
        self.tool_results = {}

    async def execute(self, tool_name: str, args: dict):
        self.executed_tools.append((tool_name, args))

        if tool_name in self.tool_results:
            return self.tool_results[tool_name]

        # Default responses
        if tool_name == "read_file":
            return {"content": f"# Content of {args.get('file_path', 'unknown')}"}
        elif tool_name == "edit_file":
            return {"success": True}
        elif tool_name == "bash":
            return {"exit_code": 0, "output": "command executed"}
        elif tool_name == "grep":
            return {"matches": ["file.py:10:match"]}

        return {"status": "success"}


class TestOrchestrationEngine:
    """Tests for the OrchestrationEngine."""

    @pytest.fixture
    def mock_ws(self):
        return MockWebSocketClient()

    @pytest.fixture
    def mock_tools(self):
        return MockToolBroker()

    @pytest.fixture
    def tmpdir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def engine(self, mock_ws, mock_tools, tmpdir):
        context_packer = ContextPacker(workspace_dir=tmpdir)
        return OrchestrationEngine(
            ws_client=mock_ws,
            tool_broker=mock_tools,
            context_packer=context_packer,
            config=OrchestrationConfig(
                max_turns=5,
                max_tool_calls_per_turn=10,
            ),
        )

    @pytest.mark.asyncio
    async def test_simple_task_completion(self, engine, mock_ws):
        """Test a simple task that completes without tool calls."""
        # Set up mock response - just text, no tool calls
        mock_ws.llm_responses = [
            {"content": "I've analyzed your request. The task is complete."}
        ]

        events = []
        async for event in engine.run_task("Say hello"):
            events.append(event)

        # Should have: thinking, text_delta, checkpoint, task_complete
        event_types = [e.type for e in events]
        assert EventType.THINKING in event_types
        assert EventType.TEXT_DELTA in event_types
        assert EventType.TASK_COMPLETE in event_types

    @pytest.mark.asyncio
    async def test_task_with_tool_calls(self, engine, mock_ws, mock_tools):
        """Test a task that uses tools."""
        # Set up mock responses
        mock_ws.llm_responses = [
            {
                "content": "Let me read the file first.",
                "tool_calls": [
                    {"id": "tc_1", "name": "read_file", "args": {"file_path": "test.py"}}
                ]
            },
            {"content": "I've read the file. Task complete."}
        ]

        events = []
        async for event in engine.run_task("Read test.py"):
            events.append(event)

        # Check tool was executed
        assert len(mock_tools.executed_tools) == 1
        assert mock_tools.executed_tools[0][0] == "read_file"

        # Check events
        event_types = [e.type for e in events]
        assert EventType.TOOL_START in event_types
        assert EventType.TOOL_END in event_types

    @pytest.mark.asyncio
    async def test_max_turns_limit(self, engine, mock_ws):
        """Test that max turns limit is enforced."""
        # Set up responses that always have tool calls (never complete)
        mock_ws.llm_responses = [
            {
                "content": f"Turn {i}",
                "tool_calls": [{"id": f"tc_{i}", "name": "bash", "args": {"command": "echo hi"}}]
            }
            for i in range(10)
        ]

        events = []
        async for event in engine.run_task("Loop forever"):
            events.append(event)

        # Should stop after max_turns (5)
        thinking_events = [e for e in events if e.type == EventType.THINKING]
        assert len(thinking_events) <= 5

    @pytest.mark.asyncio
    async def test_checkpoint_creation(self, engine, mock_ws):
        """Test that checkpoints are created."""
        mock_ws.llm_responses = [
            {"content": "Turn 1", "tool_calls": [{"id": "tc_1", "name": "bash", "args": {"command": "echo 1"}}]},
            {"content": "Turn 2", "tool_calls": [{"id": "tc_2", "name": "bash", "args": {"command": "echo 2"}}]},
            {"content": "Turn 3", "tool_calls": [{"id": "tc_3", "name": "bash", "args": {"command": "echo 3"}}]},
            {"content": "Done."}
        ]

        events = []
        async for event in engine.run_task("Do something"):
            events.append(event)

        # Check checkpoint was sent (every 3 turns or on completion)
        checkpoint_sends = [m for m in mock_ws.sent_messages if m[0] == "checkpoint"]
        assert len(checkpoint_sends) >= 1

    @pytest.mark.asyncio
    async def test_cancel(self, engine, mock_ws):
        """Test task cancellation."""
        # Set up a long-running task
        mock_ws.llm_responses = [
            {"content": f"Turn {i}", "tool_calls": [{"id": f"tc_{i}", "name": "bash", "args": {"command": "sleep 1"}}]}
            for i in range(10)
        ]

        # Start task in background
        task = asyncio.create_task(self._collect_events(engine, "Long task"))

        # Cancel after a short delay
        await asyncio.sleep(0.05)
        await engine.cancel()

        # Check cancel was sent
        cancel_sends = [m for m in mock_ws.sent_messages if m[0] == "llm_cancel"]
        assert len(cancel_sends) >= 1

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _collect_events(self, engine, prompt):
        events = []
        async for event in engine.run_task(prompt):
            events.append(event)
        return events

    @pytest.mark.asyncio
    async def test_edit_file_proposal(self, engine, mock_ws, mock_tools):
        """Test that edit_file generates an edit proposal event."""
        mock_ws.llm_responses = [
            {
                "content": "I'll fix the bug.",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "name": "edit_file",
                        "args": {
                            "file_path": "bug.py",
                            "old_string": "bug",
                            "new_string": "fix"
                        }
                    }
                ]
            },
            {"content": "Fixed!"}
        ]

        events = []
        async for event in engine.run_task("Fix the bug"):
            events.append(event)

        # Check for edit proposal event
        edit_proposals = [e for e in events if e.type == EventType.EDIT_PROPOSAL]
        assert len(edit_proposals) == 1
        assert edit_proposals[0].file_path == "bug.py"


class TestOrchestrationConfig:
    """Tests for OrchestrationConfig."""

    def test_default_config(self):
        """Test default configuration values."""
        config = OrchestrationConfig()

        assert config.model_tier == "standard"
        assert config.max_turns == 50
        assert config.max_tool_calls_per_turn == 30

    def test_custom_config(self):
        """Test custom configuration."""
        config = OrchestrationConfig(
            model_tier="fast",
            max_turns=10,
            max_tool_calls_per_turn=5,
        )

        assert config.model_tier == "fast"
        assert config.max_turns == 10
        assert config.max_tool_calls_per_turn == 5


class TestSessionKeyManager:
    """Tests for session key management."""

    def test_key_derivation(self):
        """Test that keys are derived consistently."""
        manager1 = SessionKeyManager(
            master_token="test-master-token",
            session_id="sess_123",
        )

        manager2 = SessionKeyManager(
            master_token="test-master-token",
            session_id="sess_123",
        )

        # Same inputs should produce same key
        assert manager1._derived_key == manager2._derived_key

    def test_different_sessions_different_keys(self):
        """Test that different sessions get different keys."""
        manager1 = SessionKeyManager(
            master_token="test-master-token",
            session_id="sess_123",
        )

        manager2 = SessionKeyManager(
            master_token="test-master-token",
            session_id="sess_456",
        )

        # Different sessions should have different keys
        assert manager1._derived_key != manager2._derived_key

    def test_encrypt_decrypt_roundtrip(self):
        """Test encryption and decryption."""
        manager = SessionKeyManager(
            master_token="test-master-token",
            session_id="sess_123",
        )

        original = b"This is sensitive checkpoint data"

        encrypted = manager.encrypt_checkpoint(original)
        decrypted = manager.decrypt_checkpoint(encrypted)

        assert decrypted == original
        assert encrypted != original  # Should be different

    def test_encryption_produces_different_ciphertext(self):
        """Test that encrypting same data twice produces different ciphertext."""
        manager = SessionKeyManager(
            master_token="test-master-token",
            session_id="sess_123",
        )

        data = b"Same data"

        encrypted1 = manager.encrypt_checkpoint(data)
        encrypted2 = manager.encrypt_checkpoint(data)

        # Due to random nonce, ciphertext should differ
        assert encrypted1 != encrypted2

        # But both should decrypt to same value
        assert manager.decrypt_checkpoint(encrypted1) == data
        assert manager.decrypt_checkpoint(encrypted2) == data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
