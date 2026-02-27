"""Skill invocation tool handlers."""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...skills import SkillRegistry

# Global instance
_skill_registry: Optional["SkillRegistry"] = None
_tool_broker = None
_workspace_dir: Optional[str] = None


def init_skill_tools(
    registry: Optional["SkillRegistry"] = None,
    tool_broker = None,
    workspace_dir: Optional[str] = None,
) -> None:
    """Initialize skill tools.

    Args:
        registry: SkillRegistry instance
        tool_broker: ToolBroker for skill execution
        workspace_dir: Workspace directory
    """
    global _skill_registry, _tool_broker, _workspace_dir

    if registry:
        _skill_registry = registry

    if tool_broker:
        _tool_broker = tool_broker

    if workspace_dir:
        _workspace_dir = workspace_dir


def set_skill_registry(registry: "SkillRegistry") -> None:
    """Set skill registry after initialization."""
    global _skill_registry
    _skill_registry = registry


async def skill_invoke_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Invoke a skill by name.

    Args:
        args: Dictionary containing:
            - skill: Skill name (e.g., "commit", "review-pr")
            - args: Optional arguments for the skill

    Returns:
        Dictionary with skill result
    """
    from ...skills import SkillRegistry, SkillLoader

    global _skill_registry, _tool_broker, _workspace_dir

    # Initialize registry if needed
    if _skill_registry is None:
        loader = SkillLoader()
        loader.load_builtin_skills()
        _skill_registry = loader.get_registry()

    skill_name = args.get("skill")
    if not skill_name:
        return {
            "status": "error",
            "error": "skill name is required",
        }

    skill_args = args.get("args", "")

    # Build context
    context = {
        "tool_broker": _tool_broker,
        "workspace_dir": _workspace_dir,
    }

    # Invoke skill
    skill = _skill_registry.get(skill_name)
    if not skill:
        # List available skills
        available = [s.name for s in _skill_registry.list_skills()]
        return {
            "status": "error",
            "error": f"Unknown skill: {skill_name}",
            "available_skills": available,
        }

    result = await skill.invoke(skill_args, context)

    return {
        "status": result.status,
        "output": result.output,
        "error": result.error,
        "metadata": result.metadata,
    }


async def list_skills_handler(args: dict[str, Any]) -> dict[str, Any]:
    """List available skills.

    Args:
        args: Dictionary containing:
            - category: Optional category filter

    Returns:
        Dictionary with skill list
    """
    from ...skills import SkillRegistry, SkillLoader

    global _skill_registry

    # Initialize registry if needed
    if _skill_registry is None:
        loader = SkillLoader()
        loader.load_builtin_skills()
        _skill_registry = loader.get_registry()

    category = args.get("category")
    skills = _skill_registry.list_skills(category)

    return {
        "status": "success",
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "category": s.category,
                "aliases": s.aliases,
            }
            for s in skills
        ],
        "total": len(skills),
    }
