"""Integration tests for the ContextPacker system."""

import pytest
import tempfile
import os
from pathlib import Path

from apps.cli.src.context.packer import ContextPacker, PackerConfig
from apps.cli.src.context.budget import TokenBudgetManager, TokenBudgetConfig
from apps.cli.src.context.compressor import ToolOutputCompressor, CompressionConfig
from apps.cli.src.context.slicer import SelectiveFileSlicer, SliceConfig, SliceRequest
from apps.cli.src.context.summary import RollingSummaryManager, SummaryConfig
from apps.cli.src.context.facts import PinnedFactsManager, FactsConfig
from apps.cli.src.context.delta import DeltaBuilder, DeltaConfig


class TestTokenBudgetManager:
    """Tests for token budget allocation."""

    def test_default_budget_allocation(self):
        """Test that default budgets are allocated correctly."""
        manager = TokenBudgetManager()

        # Check total budget
        assert manager.config.total_context_window == 100_000

        # Allocate some tokens
        manager.allocate("system_prompt", 1500)
        manager.allocate("rolling_summary", 3000)

        summary = manager.get_summary()
        assert summary["system_prompt"]["used"] == 1500
        assert summary["rolling_summary"]["used"] == 3000

    def test_budget_remaining(self):
        """Test remaining budget calculation."""
        manager = TokenBudgetManager(TokenBudgetConfig(
            total_context_window=10000,
            system_prompt=2000,
            rolling_summary=2000,
        ))

        manager.allocate("system_prompt", 1000)
        remaining = manager.get_remaining("system_prompt")
        assert remaining == 1000

    def test_budget_overflow_warning(self):
        """Test that overflowing budget logs warning but doesn't crash."""
        manager = TokenBudgetManager(TokenBudgetConfig(
            system_prompt=100,
        ))

        # This should not raise, just log warning
        manager.allocate("system_prompt", 200)
        assert manager.get_summary()["system_prompt"]["used"] == 200


class TestToolOutputCompressor:
    """Tests for tool output compression."""

    def test_compress_read_file(self):
        """Test compression of read_file output."""
        compressor = ToolOutputCompressor()

        # Create a large file content
        content = "x" * 10000
        result = {
            "status": "success",
            "result": {"content": content}
        }

        compressed = compressor.compress("read_file", result)

        # Should be truncated
        assert len(compressed["result"]["content"]) < len(content)
        assert "truncated" in compressed["result"]["content"]

    def test_compress_grep_output(self):
        """Test compression of grep output."""
        compressor = ToolOutputCompressor()

        # Create many matches
        matches = [f"file{i}.py:10:match" for i in range(100)]
        result = {
            "status": "success",
            "result": {"matches": matches}
        }

        compressed = compressor.compress("grep", result)

        # Should limit number of matches
        assert len(compressed["result"]["matches"]) <= 50

    def test_preserve_error_messages(self):
        """Test that error messages are preserved."""
        compressor = ToolOutputCompressor()

        result = {
            "status": "error",
            "error": "File not found: /path/to/file.py"
        }

        compressed = compressor.compress("read_file", result)

        assert compressed["status"] == "error"
        assert "File not found" in compressed["error"]


class TestSelectiveFileSlicer:
    """Tests for file slicing."""

    def test_slice_around_line(self):
        """Test slicing around a specific line."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a test file
            test_file = Path(tmpdir) / "test.py"
            lines = [f"line {i}\n" for i in range(100)]
            test_file.write_text("".join(lines))

            slicer = SelectiveFileSlicer(tmpdir)

            requests = [SliceRequest(
                file_path=str(test_file),
                line_number=50,
                reason="grep_hit",
                priority=1,
            )]

            slices = slicer.slice(requests)

            assert len(slices) == 1
            assert "line 50" in slices[0].content

    def test_merge_overlapping_slices(self):
        """Test that overlapping slices are merged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.py"
            lines = [f"line {i}\n" for i in range(100)]
            test_file.write_text("".join(lines))

            slicer = SelectiveFileSlicer(tmpdir)

            # Two requests close together should merge
            requests = [
                SliceRequest(file_path=str(test_file), line_number=50, reason="grep_hit", priority=1),
                SliceRequest(file_path=str(test_file), line_number=52, reason="grep_hit", priority=1),
            ]

            slices = slicer.slice(requests)

            # Should be merged into one slice
            assert len(slices) == 1


class TestRollingSummaryManager:
    """Tests for rolling summary."""

    def test_summary_updates(self):
        """Test that summary updates correctly."""
        manager = RollingSummaryManager()

        manager.set_task("Fix the authentication bug")

        manager.process_turn(
            assistant_content="I'll read the auth file first",
            tool_calls=[{"tool_name": "read_file", "args": {"file_path": "auth.py"}}],
            tool_results=[{"tool_name": "read_file", "status": "success", "result": {}}],
        )

        assert manager.summary.task_description == "Fix the authentication bug"
        assert manager.summary.turn_count == 1

    def test_error_recording(self):
        """Test that errors are recorded."""
        manager = RollingSummaryManager()

        manager.record_error("ImportError: No module named 'foo'")

        assert len(manager.summary.errors_encountered) == 1
        assert "ImportError" in manager.summary.errors_encountered[0]


class TestPinnedFactsManager:
    """Tests for pinned facts."""

    def test_add_and_retrieve_fact(self):
        """Test adding and retrieving facts."""
        manager = PinnedFactsManager()

        manager.add_fact("config_file", "/src/config.py", category="file_path")

        value = manager.get_fact("config_file")
        assert value == "/src/config.py"

    def test_fact_eviction(self):
        """Test that low-priority facts are evicted."""
        manager = PinnedFactsManager(FactsConfig(max_facts=5))

        # Add more facts than limit
        for i in range(10):
            manager.add_fact(f"fact_{i}", f"value_{i}", importance=float(i))

        # Should only have 5 facts
        assert len(manager.facts) == 5

        # Higher importance facts should remain
        assert manager.get_fact("fact_9") is not None

    def test_extract_from_content(self):
        """Test fact extraction from content."""
        manager = PinnedFactsManager()

        content = 'The main config is at "src/config.py" and it handles authentication.'
        manager.extract_from_content(content, role="assistant")

        # Should extract file path
        facts = manager.facts
        assert len(facts) > 0


class TestDeltaBuilder:
    """Tests for delta building."""

    def test_add_messages(self):
        """Test adding messages to delta."""
        builder = DeltaBuilder()

        builder.add_user_message("Hello")
        builder.add_assistant_message("Hi there!", tool_calls=[])

        assert builder.message_count == 2

    def test_checkpoint_marking(self):
        """Test checkpoint marking."""
        builder = DeltaBuilder()

        builder.add_user_message("Message 1")
        builder.add_assistant_message("Response 1")
        builder.mark_checkpoint()

        builder.add_user_message("Message 2")

        # Only message after checkpoint should be in delta
        assert builder.message_count == 1

    def test_build_delta(self):
        """Test building the delta."""
        builder = DeltaBuilder()

        builder.add_user_message("Fix the bug")
        builder.add_assistant_message("I'll help with that")

        delta = builder.build()

        assert len(delta.messages) == 2
        assert delta.token_count > 0


class TestContextPacker:
    """Integration tests for the full ContextPacker."""

    def test_full_packing_flow(self):
        """Test the complete context packing flow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            packer = ContextPacker(workspace_dir=tmpdir)

            # Set task
            packer.set_task("Implement user authentication")

            # Process user message
            packer.process_user_message("Add login functionality to auth.py")

            # Process assistant response
            packer.process_assistant_response(
                "I'll create the login function.",
                tool_calls=[{"tool_name": "read_file", "args": {"file_path": "auth.py"}}],
            )

            # Process tool result
            packer.process_tool_result(
                tool_call_id="tc_001",
                tool_name="read_file",
                args={"file_path": "auth.py"},
                result={"status": "success", "result": {"content": "# Auth module"}},
            )

            # Build pack and delta
            pack, delta = packer.build()

            assert pack.rolling_summary is not None
            assert pack.token_count > 0
            assert delta.token_count > 0

    def test_serialization(self):
        """Test state serialization and loading."""
        with tempfile.TemporaryDirectory() as tmpdir:
            packer1 = ContextPacker(workspace_dir=tmpdir)

            packer1.set_task("Test task")
            packer1.process_user_message("Hello")

            # Serialize
            state = packer1.serialize_state()

            # Create new packer and load
            packer2 = ContextPacker(workspace_dir=tmpdir)
            packer2.load_state(state)

            # Should have same task
            assert packer2.summary.summary.task_description == "Test task"

    def test_reset(self):
        """Test reset functionality."""
        with tempfile.TemporaryDirectory() as tmpdir:
            packer = ContextPacker(workspace_dir=tmpdir)

            packer.set_task("Test task")
            packer.process_user_message("Hello")

            packer.reset()

            assert packer.summary.summary.task_description == ""
            assert packer.delta.message_count == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
