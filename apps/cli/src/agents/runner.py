"""Agent execution engine."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, AsyncGenerator, Callable, Dict, Optional, TYPE_CHECKING

from .base import BaseAgent, AgentContext, AgentResult, AgentModelTier
from .registry import AgentRegistry
from .session import AgentSession, AgentSessionStore

if TYPE_CHECKING:
    from ..tools.broker import ToolBroker
    from ..tasks.background import BackgroundTaskRunner

logger = logging.getLogger(__name__)


class AgentRunner:
    """Executes agents with tool access and turn management.

    The runner handles:
    - Agent initialization and context setup
    - Tool execution with safety checks
    - Turn counting and limits
    - Result collection and formatting
    """

    def __init__(
        self,
        registry: Optional[AgentRegistry] = None,
        tool_broker: Optional["ToolBroker"] = None,
        llm_callback: Optional[Callable] = None,
        session_store: Optional[AgentSessionStore] = None,
        background_runner: Optional["BackgroundTaskRunner"] = None,
    ):
        """Initialize the agent runner.

        Args:
            registry: AgentRegistry instance
            tool_broker: ToolBroker for executing tools
            llm_callback: Callback for LLM requests (async function)
            session_store: AgentSessionStore for persistence/resume
            background_runner: BackgroundTaskRunner for async execution
        """
        self.registry = registry or AgentRegistry()
        self.tool_broker = tool_broker
        self.llm_callback = llm_callback
        self.session_store = session_store or AgentSessionStore()
        self.background_runner = background_runner

    def set_background_runner(self, runner: "BackgroundTaskRunner") -> None:
        """Set the background runner after initialization."""
        self.background_runner = runner

    def set_llm_callback(self, callback: Callable) -> None:
        """Set the LLM callback after initialization."""
        self.llm_callback = callback

    async def run(
        self,
        agent_type: str,
        prompt: str,
        workspace_dir: str,
        session_id: Optional[str] = None,
        conversation_history: Optional[list[dict]] = None,
        max_turns: Optional[int] = None,
        model_tier: Optional[AgentModelTier] = None,
        run_in_background: bool = False,
    ) -> AgentResult:
        """Run an agent with the given prompt.

        Args:
            agent_type: Agent type name (e.g., "Explore", "python-expert")
            prompt: Task/prompt for the agent
            workspace_dir: Working directory
            session_id: Parent session ID
            conversation_history: Previous messages (if agent has context access)
            max_turns: Override max turns
            model_tier: Override model tier
            run_in_background: If True, return immediately with task ID

        Returns:
            AgentResult with output and metadata
        """
        # Get agent
        agent = self.registry.get(agent_type)
        if not agent:
            return AgentResult(
                agent_id=f"error_{agent_type}",
                status="failed",
                error=f"Unknown agent type: {agent_type}",
            )

        # Build context
        context = AgentContext(
            prompt=prompt,
            workspace_dir=workspace_dir,
            session_id=session_id,
            conversation_history=conversation_history or [],
            max_turns=max_turns or agent.max_turns,
            model_tier=model_tier or agent.model_tier,
            allowed_tools=agent.get_allowed_tools(AgentContext(prompt=prompt, workspace_dir=workspace_dir)),
            read_only=agent.read_only,
        )

        # Validate context
        error = agent.validate_context(context)
        if error:
            return AgentResult(
                agent_id=agent.agent_id,
                status="failed",
                error=error,
            )

        # Execute agent
        try:
            await agent.pre_execute(context)
            result = await self._execute_agent(agent, context)
            result = await agent.post_execute(context, result)
            return result
        except Exception as e:
            logger.exception(f"Agent {agent.name} failed")
            return AgentResult(
                agent_id=agent.agent_id,
                status="failed",
                error=str(e),
            )

    async def run_in_background(
        self,
        agent_type: str,
        prompt: str,
        workspace_dir: str,
        session_id: Optional[str] = None,
        max_turns: Optional[int] = None,
        model_tier: Optional[AgentModelTier] = None,
    ) -> dict[str, Any]:
        """Run an agent in the background.

        Args:
            agent_type: Agent type name
            prompt: Task/prompt for the agent
            workspace_dir: Working directory
            session_id: Parent session ID
            max_turns: Override max turns
            model_tier: Override model tier

        Returns:
            Dict with task_id and output_file
        """
        if not self.background_runner:
            return {
                "status": "error",
                "error": "Background runner not configured",
            }

        # Get agent to validate it exists
        agent = self.registry.get(agent_type)
        if not agent:
            return {
                "status": "error",
                "error": f"Unknown agent type: {agent_type}",
            }

        # Create the agent session for tracking
        agent_session = AgentSession(
            agent_id=agent.agent_id,
            agent_type=agent_type,
            prompt=prompt,
            workspace_dir=workspace_dir,
            max_turns=max_turns or agent.max_turns,
            session_id=session_id,
            model_tier=(model_tier or agent.model_tier).value,
            status="running",
        )
        self.session_store.save(agent_session)

        # Spawn the agent execution as a background coroutine
        async def _run_agent():
            result = await self.run(
                agent_type=agent_type,
                prompt=prompt,
                workspace_dir=workspace_dir,
                session_id=session_id,
                max_turns=max_turns,
                model_tier=model_tier,
            )
            # Update session with result
            agent_session.status = result.status
            agent_session.last_output = result.output
            agent_session.turns_used = result.turns_used
            agent_session.tools_called = result.tools_called
            agent_session.files_modified = result.files_modified
            agent_session.tokens_used = result.tokens_used
            agent_session.error = result.error
            agent_session.completed_at = datetime.utcnow()
            self.session_store.save(agent_session)
            return json.dumps(result.to_dict(), indent=2)

        bg_task = await self.background_runner.spawn_coroutine(
            coro=_run_agent,
            name=f"agent:{agent_type}",
            task_id=None,
        )

        return {
            "status": "running",
            "task_id": bg_task.id,
            "agent_id": agent.agent_id,
            "output_file": bg_task.output_file,
            "message": f"Agent '{agent_type}' running in background",
        }

    async def resume(
        self,
        agent_id: str,
        additional_prompt: Optional[str] = None,
    ) -> AgentResult:
        """Resume a paused agent session.

        Args:
            agent_id: ID of the agent session to resume
            additional_prompt: Optional additional context/instruction

        Returns:
            AgentResult from continued execution
        """
        # Load the session
        session = self.session_store.load(agent_id)
        if not session:
            return AgentResult(
                agent_id=agent_id,
                status="failed",
                error=f"Agent session not found: {agent_id}",
            )

        if session.status not in ("paused", "running"):
            return AgentResult(
                agent_id=agent_id,
                status="failed",
                error=f"Agent session cannot be resumed (status: {session.status})",
            )

        # Get the agent
        agent = self.registry.get(session.agent_type)
        if not agent:
            return AgentResult(
                agent_id=agent_id,
                status="failed",
                error=f"Unknown agent type: {session.agent_type}",
            )

        # Build context from session
        context = AgentContext(
            prompt=session.prompt,
            workspace_dir=session.workspace_dir,
            session_id=session.session_id,
            parent_agent_id=session.parent_agent_id,
            conversation_history=session.messages,
            max_turns=session.max_turns - session.turn_count,  # Remaining turns
            model_tier=AgentModelTier(session.model_tier),
            allowed_tools=agent.get_allowed_tools(AgentContext(
                prompt=session.prompt,
                workspace_dir=session.workspace_dir,
            )),
            read_only=agent.read_only,
        )

        # If additional prompt provided, add it as a user message
        if additional_prompt:
            session.messages.append({
                "role": "user",
                "content": additional_prompt,
            })

        # Update session status
        session.status = "running"
        self.session_store.save(session)

        # Continue execution
        try:
            result = await self._execute_agent_with_session(agent, context, session)

            # Update session on completion
            session.status = result.status
            session.last_output = result.output
            session.completed_at = datetime.utcnow()
            self.session_store.save(session)

            return result

        except Exception as e:
            logger.exception(f"Agent resume failed: {agent_id}")
            session.status = "failed"
            session.error = str(e)
            self.session_store.save(session)

            return AgentResult(
                agent_id=agent_id,
                status="failed",
                error=str(e),
            )

    async def pause(self, agent_id: str) -> bool:
        """Pause a running agent (mark for resume later).

        Args:
            agent_id: ID of the agent to pause

        Returns:
            True if paused, False otherwise
        """
        session = self.session_store.load(agent_id)
        if not session:
            return False

        if session.status == "running":
            session.status = "paused"
            self.session_store.save(session)
            return True

        return False

    def get_session(self, agent_id: str) -> Optional[AgentSession]:
        """Get an agent session by ID.

        Args:
            agent_id: Agent session ID

        Returns:
            AgentSession if found
        """
        return self.session_store.load(agent_id)

    def list_sessions(
        self,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list[AgentSession]:
        """List agent sessions.

        Args:
            status: Filter by status
            limit: Maximum number to return

        Returns:
            List of AgentSession objects
        """
        return self.session_store.list_sessions(status=status, limit=limit)

    async def _execute_agent_with_session(
        self,
        agent: BaseAgent,
        context: AgentContext,
        session: AgentSession,
    ) -> AgentResult:
        """Execute agent while updating session state.

        Args:
            agent: Agent instance
            context: Agent context
            session: Session to update

        Returns:
            AgentResult
        """
        result = AgentResult(
            agent_id=agent.agent_id,
            status="running",
            started_at=session.started_at,
            turns_used=session.turn_count,
            tokens_used=session.tokens_used,
            tools_called=list(session.tools_called),
            files_modified=list(session.files_modified),
        )

        # Build system prompt
        system_prompt = session.system_prompt or agent.get_system_prompt(context)

        # Use existing messages from session
        messages = list(session.messages)

        # Get allowed tools
        allowed_tools = agent.get_allowed_tools(context)
        tool_definitions = self._get_tool_definitions(allowed_tools)

        # Continue the agent loop
        while result.turns_used < session.max_turns:
            result.turns_used += 1
            session.turn_count = result.turns_used

            # Call LLM
            if not self.llm_callback:
                result.status = "failed"
                result.error = "No LLM callback configured"
                break

            try:
                response = await self.llm_callback(
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tool_definitions,
                    model_tier=context.model_tier.value,
                )
            except Exception as e:
                logger.exception("LLM call failed")
                result.status = "failed"
                result.error = f"LLM error: {str(e)}"
                break

            # Process response
            assistant_message = {
                "role": "assistant",
                "content": response.get("content", ""),
            }

            tool_calls = response.get("tool_calls", [])
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls

            messages.append(assistant_message)
            session.messages = messages

            # If no tool calls, we're done
            if not tool_calls:
                result.output = response.get("content", "")
                result.status = "completed"
                break

            # Execute tool calls
            tool_results = []
            for tool_call in tool_calls:
                tool_name = tool_call.get("name")
                tool_args = tool_call.get("arguments", {})
                tool_id = tool_call.get("id", f"tool_{result.turns_used}")

                result.tools_called.append(tool_name)
                session.tools_called = result.tools_called

                # Check if tool is allowed
                if allowed_tools and tool_name not in allowed_tools:
                    tool_results.append({
                        "tool_call_id": tool_id,
                        "role": "tool",
                        "content": f"Error: Tool '{tool_name}' is not allowed",
                    })
                    continue

                # Execute tool
                try:
                    if self.tool_broker:
                        tool_result = await self.tool_broker.execute(tool_name, tool_args)
                        content = self._format_tool_result(tool_result)

                        if tool_name in ("write_file", "edit_file"):
                            if "path" in tool_result:
                                result.files_modified.append(tool_result["path"])
                                session.files_modified = result.files_modified
                    else:
                        content = "Error: No tool broker configured"
                except Exception as e:
                    logger.exception(f"Tool {tool_name} failed")
                    content = f"Error: {str(e)}"

                tool_results.append({
                    "tool_call_id": tool_id,
                    "role": "tool",
                    "content": content,
                })

            messages.extend(tool_results)
            session.messages = messages

            # Save session after each turn
            self.session_store.save(session)

            result.tokens_used += len(str(messages[-1])) // 4
            session.tokens_used = result.tokens_used

        result.completed_at = datetime.utcnow()

        if result.turns_used >= session.max_turns and result.status != "completed":
            result.status = "max_turns_reached"

        return result

    async def _execute_agent(
        self,
        agent: BaseAgent,
        context: AgentContext,
    ) -> AgentResult:
        """Execute the agent's main loop.

        Args:
            agent: Agent instance
            context: Agent context

        Returns:
            AgentResult
        """
        result = AgentResult(
            agent_id=agent.agent_id,
            status="running",
            started_at=datetime.utcnow(),
        )

        # Build system prompt
        system_prompt = agent.get_system_prompt(context)

        # Build messages
        messages = []

        # Add conversation history if agent has context access
        if agent.has_context_access and context.conversation_history:
            messages.extend(context.conversation_history)

        # Add the agent's task
        messages.append({
            "role": "user",
            "content": context.prompt,
        })

        # Get allowed tools
        allowed_tools = agent.get_allowed_tools(context)

        # Build tool definitions
        tool_definitions = self._get_tool_definitions(allowed_tools)

        # Main agent loop
        turn = 0
        final_output = ""

        while turn < context.max_turns:
            turn += 1

            # Call LLM
            if not self.llm_callback:
                result.status = "failed"
                result.error = "No LLM callback configured"
                break

            try:
                response = await self.llm_callback(
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tool_definitions,
                    model_tier=context.model_tier.value,
                )
            except Exception as e:
                logger.exception("LLM call failed")
                result.status = "failed"
                result.error = f"LLM error: {str(e)}"
                break

            # Process response
            assistant_message = {
                "role": "assistant",
                "content": response.get("content", ""),
            }

            # Check for tool calls
            tool_calls = response.get("tool_calls", [])
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls

            messages.append(assistant_message)

            # If no tool calls, we're done
            if not tool_calls:
                final_output = response.get("content", "")
                result.status = "completed"
                break

            # Execute tool calls
            tool_results = []
            for tool_call in tool_calls:
                tool_name = tool_call.get("name")
                tool_args = tool_call.get("arguments", {})
                tool_id = tool_call.get("id", f"tool_{turn}")

                result.tools_called.append(tool_name)

                # Check if tool is allowed
                if allowed_tools and tool_name not in allowed_tools:
                    tool_results.append({
                        "tool_call_id": tool_id,
                        "role": "tool",
                        "content": f"Error: Tool '{tool_name}' is not allowed for this agent",
                    })
                    continue

                # Execute tool
                try:
                    if self.tool_broker:
                        tool_result = await self.tool_broker.execute(tool_name, tool_args)
                        content = self._format_tool_result(tool_result)

                        # Track file modifications
                        if tool_name in ("write_file", "edit_file"):
                            if "path" in tool_result:
                                result.files_modified.append(tool_result["path"])
                    else:
                        content = f"Error: No tool broker configured"
                except Exception as e:
                    logger.exception(f"Tool {tool_name} failed")
                    content = f"Error: {str(e)}"

                tool_results.append({
                    "tool_call_id": tool_id,
                    "role": "tool",
                    "content": content,
                })

            # Add tool results to messages
            messages.extend(tool_results)

            # Update token estimate (rough)
            result.tokens_used += len(str(messages[-1])) // 4

        # Update result
        result.turns_used = turn
        result.output = final_output
        result.completed_at = datetime.utcnow()

        if turn >= context.max_turns and result.status != "completed":
            result.status = "max_turns_reached"

        return result

    def _get_tool_definitions(
        self,
        allowed_tools: Optional[list[str]] = None,
    ) -> list[dict]:
        """Get tool definitions for the LLM.

        Args:
            allowed_tools: List of allowed tool names

        Returns:
            List of tool definition dicts
        """
        # Full tool definitions
        all_tools = {
            "read_file": {
                "name": "read_file",
                "description": "Read the contents of a file",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the file"},
                        "offset": {"type": "integer", "description": "Line offset to start reading"},
                        "limit": {"type": "integer", "description": "Max lines to read"},
                    },
                    "required": ["file_path"],
                },
            },
            "edit_file": {
                "name": "edit_file",
                "description": "Edit a file by replacing text",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the file"},
                        "old_string": {"type": "string", "description": "Text to replace"},
                        "new_string": {"type": "string", "description": "Replacement text"},
                        "replace_all": {"type": "boolean", "description": "Replace all occurrences"},
                    },
                    "required": ["file_path", "old_string", "new_string"],
                },
            },
            "write_file": {
                "name": "write_file",
                "description": "Write content to a file",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the file"},
                        "content": {"type": "string", "description": "Content to write"},
                    },
                    "required": ["file_path", "content"],
                },
            },
            "glob": {
                "name": "glob",
                "description": "Find files matching a glob pattern",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Glob pattern"},
                        "path": {"type": "string", "description": "Base directory"},
                    },
                    "required": ["pattern"],
                },
            },
            "grep": {
                "name": "grep",
                "description": "Search for text in files",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Search pattern (regex)"},
                        "path": {"type": "string", "description": "Directory to search"},
                        "output_mode": {"type": "string", "enum": ["files_with_matches", "content", "count"]},
                    },
                    "required": ["pattern"],
                },
            },
            "bash": {
                "name": "bash",
                "description": "Execute a shell command",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Command to execute"},
                        "timeout": {"type": "integer", "description": "Timeout in seconds"},
                    },
                    "required": ["command"],
                },
            },
            "list_directory": {
                "name": "list_directory",
                "description": "List contents of a directory",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Directory path"},
                    },
                },
            },
            "web_fetch": {
                "name": "web_fetch",
                "description": "Fetch content from a URL",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to fetch"},
                        "prompt": {"type": "string", "description": "Extraction prompt"},
                    },
                    "required": ["url"],
                },
            },
            "web_search": {
                "name": "web_search",
                "description": "Search the web",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "max_results": {"type": "integer", "description": "Max results"},
                    },
                    "required": ["query"],
                },
            },
            "TaskCreate": {
                "name": "TaskCreate",
                "description": "Create a new task",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string", "description": "Task title"},
                        "description": {"type": "string", "description": "Task description"},
                        "activeForm": {"type": "string", "description": "Present continuous form"},
                    },
                    "required": ["subject", "description"],
                },
            },
            "TaskList": {
                "name": "TaskList",
                "description": "List all tasks",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            "TaskGet": {
                "name": "TaskGet",
                "description": "Get task details",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "taskId": {"type": "string", "description": "Task ID"},
                    },
                    "required": ["taskId"],
                },
            },
            "TaskUpdate": {
                "name": "TaskUpdate",
                "description": "Update a task",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "taskId": {"type": "string", "description": "Task ID"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]},
                    },
                    "required": ["taskId"],
                },
            },
        }

        # Filter to allowed tools
        if allowed_tools:
            return [all_tools[name] for name in allowed_tools if name in all_tools]

        return list(all_tools.values())

    def _format_tool_result(self, result: dict) -> str:
        """Format a tool result for the LLM.

        Args:
            result: Tool result dict

        Returns:
            Formatted string
        """
        if "error" in result:
            return f"Error: {result['error']}"

        if "content" in result:
            return result["content"]

        if "output" in result:
            return result["output"]

        if "matches" in result:
            return "\n".join(result["matches"])

        # Generic formatting
        import json
        return json.dumps(result, indent=2)
