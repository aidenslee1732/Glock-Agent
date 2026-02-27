"""Remember skill - stores facts in memory."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..base import Skill, SkillResult


# Storage location
MEMORY_FILE = Path.home() / ".glock" / "memory.json"


def _load_memories() -> list[dict]:
    """Load memories from file."""
    if not MEMORY_FILE.exists():
        return []

    try:
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def _save_memories(memories: list[dict]) -> None:
    """Save memories to file."""
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MEMORY_FILE, "w") as f:
        json.dump(memories, f, indent=2)


async def remember_handler(args: str, context: dict[str, Any]) -> SkillResult:
    """Store a fact in memory.

    Args:
        args: Fact to remember
        context: Execution context

    Returns:
        SkillResult with confirmation
    """
    fact = args.strip()

    if not fact:
        # List memories
        memories = _load_memories()
        if not memories:
            return SkillResult(
                status="completed",
                output="No memories stored. Use /remember <fact> to store something.",
            )

        lines = ["## Stored Memories", ""]
        for i, mem in enumerate(memories[-10:], 1):
            lines.append(f"{i}. {mem['fact']}")
            lines.append(f"   _{mem['timestamp']}_")

        if len(memories) > 10:
            lines.append(f"\n... and {len(memories) - 10} more")

        return SkillResult(
            status="completed",
            output="\n".join(lines),
            metadata={"total_memories": len(memories)},
        )

    # Store new memory
    memories = _load_memories()

    new_memory = {
        "fact": fact,
        "timestamp": datetime.utcnow().isoformat(),
        "workspace": context.get("workspace_dir", "unknown"),
    }

    memories.append(new_memory)
    _save_memories(memories)

    return SkillResult(
        status="completed",
        output=f"Remembered: {fact}",
        metadata={"memory_id": len(memories)},
    )


async def forget_handler(args: str, context: dict[str, Any]) -> SkillResult:
    """Remove a memory.

    Args:
        args: Memory index or search term
        context: Execution context

    Returns:
        SkillResult with confirmation
    """
    search = args.strip()

    if not search:
        return SkillResult(
            status="failed",
            error="Please provide a memory number or search term.",
        )

    memories = _load_memories()

    if not memories:
        return SkillResult(
            status="completed",
            output="No memories to forget.",
        )

    # Try as index
    if search.isdigit():
        idx = int(search) - 1
        if 0 <= idx < len(memories):
            removed = memories.pop(idx)
            _save_memories(memories)
            return SkillResult(
                status="completed",
                output=f"Forgot: {removed['fact']}",
            )
        else:
            return SkillResult(
                status="failed",
                error=f"Invalid memory number. Use 1-{len(memories)}.",
            )

    # Search and remove matching
    original_count = len(memories)
    memories = [m for m in memories if search.lower() not in m["fact"].lower()]
    removed_count = original_count - len(memories)

    if removed_count > 0:
        _save_memories(memories)
        return SkillResult(
            status="completed",
            output=f"Forgot {removed_count} memories matching '{search}'.",
        )
    else:
        return SkillResult(
            status="completed",
            output=f"No memories found matching '{search}'.",
        )


def get_skill() -> Skill:
    """Get the remember skill."""
    return Skill(
        name="remember",
        description="Store a fact in memory for future reference",
        handler=remember_handler,
        aliases=["mem", "save"],
        category="memory",
        requires_tools=[],
    )


def get_forget_skill() -> Skill:
    """Get the forget skill."""
    return Skill(
        name="forget",
        description="Remove a memory",
        handler=forget_handler,
        aliases=["rm-mem"],
        category="memory",
        requires_tools=[],
    )
