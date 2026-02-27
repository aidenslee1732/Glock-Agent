"""Agent spawning tool handlers."""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...agents import AgentRegistry, AgentRunner

# Global instances
_agent_registry: Optional["AgentRegistry"] = None
_agent_runner: Optional["AgentRunner"] = None
_workspace_dir: Optional[str] = None


def init_agent_tools(
    registry: Optional["AgentRegistry"] = None,
    runner: Optional["AgentRunner"] = None,
    workspace_dir: Optional[str] = None,
) -> None:
    """Initialize agent tools.

    Args:
        registry: AgentRegistry instance
        runner: AgentRunner instance
        workspace_dir: Workspace directory
    """
    global _agent_registry, _agent_runner, _workspace_dir

    if registry:
        _agent_registry = registry

    if runner:
        _agent_runner = runner

    if workspace_dir:
        _workspace_dir = workspace_dir


def set_agent_registry(registry: "AgentRegistry") -> None:
    """Set agent registry after initialization."""
    global _agent_registry
    _agent_registry = registry


def set_agent_runner(runner: "AgentRunner") -> None:
    """Set agent runner after initialization."""
    global _agent_runner
    _agent_runner = runner


def set_background_runner(runner) -> None:
    """Set background runner on the agent runner."""
    global _agent_runner
    if _agent_runner:
        _agent_runner.set_background_runner(runner)


async def task_spawn_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Spawn a specialized agent to handle a task.

    This is the "Task" tool that launches subagents.

    Args:
        args: Dictionary containing:
            - description: Short description of the task (3-5 words)
            - prompt: The task for the agent to perform
            - subagent_type: Type of agent to spawn
            - max_turns: Optional max turns override
            - model: Optional model tier ("sonnet", "opus", "haiku")
            - run_in_background: Run asynchronously
            - resume: Optional agent ID to resume

    Returns:
        Dictionary with agent result or background task info
    """
    from ...agents import AgentRegistry, AgentRunner
    from ...agents.base import AgentModelTier

    global _agent_registry, _agent_runner, _workspace_dir

    # Initialize if needed
    if _agent_registry is None:
        _agent_registry = AgentRegistry()

    # Validate required args
    prompt = args.get("prompt")
    if not prompt:
        return {
            "status": "error",
            "error": "prompt is required",
        }

    subagent_type = args.get("subagent_type")
    if not subagent_type:
        return {
            "status": "error",
            "error": "subagent_type is required",
        }

    description = args.get("description", "Running agent task")

    # Check if agent type exists
    agent = _agent_registry.get(subagent_type)
    if not agent:
        # List available agents
        available = [c.name for c in _agent_registry.list_agents()]
        return {
            "status": "error",
            "error": f"Unknown agent type: {subagent_type}",
            "available_agents": available[:20],
        }

    # Parse model tier
    model_tier = None
    model_arg = args.get("model")
    if model_arg:
        model_map = {
            "haiku": AgentModelTier.FAST,
            "sonnet": AgentModelTier.STANDARD,
            "opus": AgentModelTier.ADVANCED,
        }
        model_tier = model_map.get(model_arg.lower())

    # Check for resume
    resume_id = args.get("resume")
    if resume_id:
        if _agent_runner is None:
            return {
                "status": "error",
                "error": "Agent runner not configured for resume",
            }

        # Resume the agent session
        additional_prompt = args.get("prompt")  # Can add more context on resume
        try:
            result = await _agent_runner.resume(
                agent_id=resume_id,
                additional_prompt=additional_prompt if additional_prompt != prompt else None,
            )

            return {
                "status": result.status,
                "agent_id": result.agent_id,
                "output": result.output,
                "turns_used": result.turns_used,
                "tools_called": result.tools_called,
                "files_modified": result.files_modified,
                "error": result.error,
                "resumed": True,
            }
        except Exception as e:
            return {
                "status": "error",
                "error": f"Failed to resume agent: {str(e)}",
            }

    # Check for background execution
    run_in_background = args.get("run_in_background", False)

    if run_in_background:
        if _agent_runner is None:
            return {
                "status": "error",
                "error": "Agent runner not configured for background execution",
            }

        # Spawn agent in background
        try:
            result = await _agent_runner.run_in_background(
                agent_type=subagent_type,
                prompt=prompt,
                workspace_dir=_workspace_dir or ".",
                max_turns=args.get("max_turns"),
                model_tier=model_tier,
            )

            return result

        except Exception as e:
            return {
                "status": "error",
                "error": f"Failed to spawn background agent: {str(e)}",
            }

    # Run synchronously
    if _agent_runner is None:
        # Create a simple runner without LLM callback
        # The actual LLM integration happens at the orchestrator level
        return {
            "status": "pending",
            "message": f"Agent '{subagent_type}' ready to execute: {description}",
            "agent_type": subagent_type,
            "prompt": prompt,
            "agent_info": {
                "name": agent.name,
                "description": agent.description,
                "max_turns": args.get("max_turns") or agent.max_turns,
                "model_tier": (model_tier or agent.model_tier).value if model_tier or agent.model_tier else "standard",
                "read_only": agent.read_only,
                "allowed_tools": agent.allowed_tools,
            },
        }

    # Execute with runner
    try:
        result = await _agent_runner.run(
            agent_type=subagent_type,
            prompt=prompt,
            workspace_dir=_workspace_dir or ".",
            max_turns=args.get("max_turns"),
            model_tier=model_tier,
        )

        return {
            "status": result.status,
            "agent_id": result.agent_id,
            "output": result.output,
            "turns_used": result.turns_used,
            "tools_called": result.tools_called,
            "files_modified": result.files_modified,
            "error": result.error,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
        }


async def list_agents_handler(args: dict[str, Any]) -> dict[str, Any]:
    """List available agent types.

    Args:
        args: Dictionary containing:
            - category: Optional category filter
            - search: Optional search query

    Returns:
        Dictionary with agent list
    """
    from ...agents import AgentRegistry

    global _agent_registry

    if _agent_registry is None:
        _agent_registry = AgentRegistry()

    category = args.get("category")
    search = args.get("search")

    if search:
        agents = _agent_registry.search(search)
    else:
        agents = _agent_registry.list_agents(category)

    return {
        "status": "success",
        "agents": [
            {
                "name": a.name,
                "description": a.description,
                "category": a.category,
                "aliases": a.aliases,
            }
            for a in agents
        ],
        "total": len(agents),
    }


async def list_resumable_agents_handler(args: dict[str, Any]) -> dict[str, Any]:
    """List agent sessions that can be resumed.

    Args:
        args: Dictionary containing:
            - limit: Maximum number to return (default 20)

    Returns:
        Dictionary with resumable sessions
    """
    global _agent_runner

    if _agent_runner is None:
        from ...agents import AgentRunner
        _agent_runner = AgentRunner()

    limit = args.get("limit", 20)
    sessions = _agent_runner.list_sessions(status="paused", limit=limit)

    return {
        "status": "success",
        "sessions": [
            {
                "agent_id": s.agent_id,
                "agent_type": s.agent_type,
                "prompt": s.prompt[:100] + "..." if len(s.prompt) > 100 else s.prompt,
                "turn_count": s.turn_count,
                "max_turns": s.max_turns,
                "last_activity": s.last_activity.isoformat(),
                "tools_called": len(s.tools_called),
                "files_modified": len(s.files_modified),
            }
            for s in sessions
        ],
        "total": len(sessions),
    }


async def get_agent_session_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Get details of an agent session.

    Args:
        args: Dictionary containing:
            - agent_id: ID of the agent session

    Returns:
        Dictionary with session details
    """
    global _agent_runner

    if _agent_runner is None:
        return {
            "status": "error",
            "error": "Agent runner not configured",
        }

    agent_id = args.get("agent_id")
    if not agent_id:
        return {
            "status": "error",
            "error": "agent_id is required",
        }

    session = _agent_runner.get_session(agent_id)
    if not session:
        return {
            "status": "error",
            "error": f"Session not found: {agent_id}",
        }

    return {
        "status": "success",
        "session": {
            "agent_id": session.agent_id,
            "agent_type": session.agent_type,
            "prompt": session.prompt,
            "status": session.status,
            "turn_count": session.turn_count,
            "max_turns": session.max_turns,
            "tokens_used": session.tokens_used,
            "tools_called": session.tools_called,
            "files_modified": session.files_modified,
            "last_output": session.last_output,
            "error": session.error,
            "started_at": session.started_at.isoformat(),
            "last_activity": session.last_activity.isoformat(),
            "completed_at": session.completed_at.isoformat() if session.completed_at else None,
        },
    }
