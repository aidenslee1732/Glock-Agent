"""Pytest configuration and fixtures."""

import pytest
import asyncio
import tempfile
import os
from pathlib import Path


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create some test files
        (Path(tmpdir) / "test.py").write_text("""
def hello():
    print("Hello, World!")

def add(a, b):
    return a + b
""")
        (Path(tmpdir) / "config.json").write_text('{"debug": true}')
        (Path(tmpdir) / "README.md").write_text("# Test Project")

        yield tmpdir


@pytest.fixture
def mock_env():
    """Set up mock environment variables."""
    original = os.environ.copy()

    os.environ.update({
        "JWT_SECRET": "test-secret-key-at-least-32-characters",
        "JWT_ISSUER": "glock.test",
        "CONTEXT_MASTER_KEY": "0" * 64,
    })

    yield

    os.environ.clear()
    os.environ.update(original)


@pytest.fixture
def sample_messages():
    """Sample conversation messages for testing."""
    return [
        {"role": "user", "content": "Fix the bug in auth.py"},
        {"role": "assistant", "content": "I'll read the file first."},
        {"role": "user", "content": "The bug is on line 45"},
    ]


@pytest.fixture
def sample_tool_results():
    """Sample tool results for testing."""
    return [
        {
            "tool_call_id": "tc_001",
            "tool_name": "read_file",
            "status": "success",
            "result": {"content": "def login(): pass"},
        },
        {
            "tool_call_id": "tc_002",
            "tool_name": "grep",
            "status": "success",
            "result": {"matches": ["auth.py:45:if password == stored:"]},
        },
    ]


@pytest.fixture
def sample_context_pack():
    """Sample context pack for testing."""
    from packages.shared_protocol.types import (
        ContextPack,
        RollingSummary,
        PinnedFact,
        FileSlice,
    )

    return ContextPack(
        rolling_summary=RollingSummary(
            task_description="Fix authentication bug",
            files_modified=["auth.py"],
            files_read=["auth.py", "config.py"],
            key_decisions=["Use constant-time comparison"],
            errors_encountered=[],
            current_state="Implementing fix",
            turn_count=3,
        ),
        pinned_facts=[
            PinnedFact(
                key="bug_location",
                value="auth.py:45",
                category="error_solution",
                importance=1.5,
            ),
        ],
        file_slices=[
            FileSlice(
                file_path="auth.py",
                start_line=40,
                end_line=50,
                content="def verify_password(pw, stored):\n    return pw == stored",
                reason="traceback",
            ),
        ],
        token_count=500,
    )
