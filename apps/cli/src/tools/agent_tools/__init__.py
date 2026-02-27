"""Agent spawning tools for Glock CLI."""

from .handlers import (
    task_spawn_handler,
    init_agent_tools,
    set_agent_registry,
    set_agent_runner,
    set_background_runner,
)

__all__ = [
    "task_spawn_handler",
    "init_agent_tools",
    "set_agent_registry",
    "set_agent_runner",
    "set_background_runner",
]
