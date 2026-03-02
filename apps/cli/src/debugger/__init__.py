"""Debugging tools for Glock CLI.

Provides DAP (Debug Adapter Protocol) integration for debugging support:
- Python debugging via debugpy
- Node.js debugging via node --inspect
- Breakpoint management
- Variable inspection
- Stack trace analysis

Usage:
    debugger = DebugManager()
    await debugger.start_debug_session("python", "/path/to/script.py")
    await debugger.set_breakpoint("/path/to/script.py", 10)
    await debugger.continue_execution()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class DebugState(Enum):
    """Debug session states."""
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class Breakpoint:
    """Represents a breakpoint in code.

    Attributes:
        id: Unique breakpoint ID
        file_path: Absolute path to the file
        line: Line number (1-indexed)
        condition: Optional condition expression
        hit_count: Number of times to hit before breaking
        enabled: Whether breakpoint is active
        verified: Whether debugger accepted the breakpoint
    """
    id: int
    file_path: str
    line: int
    condition: Optional[str] = None
    hit_count: int = 0
    enabled: bool = True
    verified: bool = False


@dataclass
class StackFrame:
    """Represents a stack frame.

    Attributes:
        id: Frame ID
        name: Function/method name
        file_path: Source file path
        line: Line number
        column: Column number
        source_text: Optional source line text
    """
    id: int
    name: str
    file_path: str
    line: int
    column: int = 0
    source_text: Optional[str] = None


@dataclass
class Variable:
    """Represents a variable in scope.

    Attributes:
        name: Variable name
        value: String representation of value
        type: Variable type name
        children_ref: Reference for expanding nested values
        is_expandable: Whether variable has children
    """
    name: str
    value: str
    type: str
    children_ref: int = 0
    is_expandable: bool = False


@dataclass
class DebugSession:
    """An active debug session.

    Attributes:
        id: Session ID
        language: Programming language (python, node, etc.)
        target_path: Path to script/program being debugged
        state: Current session state
        process: Subprocess if launched
        port: Debug adapter port
        breakpoints: Active breakpoints
    """
    id: str
    language: str
    target_path: str
    state: DebugState = DebugState.IDLE
    process: Optional[subprocess.Popen] = None
    port: int = 0
    breakpoints: dict[str, list[Breakpoint]] = field(default_factory=dict)
    _seq: int = 0
    _pending_responses: dict[int, asyncio.Future] = field(default_factory=dict)

    def next_seq(self) -> int:
        """Get next sequence number for DAP messages."""
        self._seq += 1
        return self._seq


class DAPClient:
    """Debug Adapter Protocol client.

    Communicates with debug adapters using the DAP protocol over TCP.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        """Initialize DAP client.

        Args:
            host: Debug adapter host
            port: Debug adapter port
        """
        self.host = host
        self.port = port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._event_handlers: dict[str, list[Callable]] = {}
        self._read_task: Optional[asyncio.Task] = None

    async def connect(self, timeout: float = 10.0) -> bool:
        """Connect to the debug adapter.

        Args:
            timeout: Connection timeout in seconds

        Returns:
            True if connected successfully
        """
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=timeout,
            )
            self._read_task = asyncio.create_task(self._read_loop())
            logger.info(f"Connected to debug adapter at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to debug adapter: {e}")
            return False

    async def disconnect(self) -> None:
        """Disconnect from the debug adapter."""
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()

        self._reader = None
        self._writer = None

    def on_event(self, event_type: str, handler: Callable) -> None:
        """Register an event handler.

        Args:
            event_type: DAP event type (stopped, output, etc.)
            handler: Callback function
        """
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    async def send_request(
        self,
        command: str,
        arguments: Optional[dict] = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send a DAP request and wait for response.

        Args:
            command: DAP command name
            arguments: Command arguments
            timeout: Response timeout

        Returns:
            Response body

        Raises:
            TimeoutError: If response not received in time
            RuntimeError: If not connected or request failed
        """
        if not self._writer:
            raise RuntimeError("Not connected to debug adapter")

        self._seq += 1
        seq = self._seq

        request = {
            "seq": seq,
            "type": "request",
            "command": command,
        }
        if arguments:
            request["arguments"] = arguments

        # Create response future
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[seq] = future

        # Send request
        content = json.dumps(request)
        message = f"Content-Length: {len(content)}\r\n\r\n{content}"
        self._writer.write(message.encode())
        await self._writer.drain()

        logger.debug(f"Sent DAP request: {command}")

        try:
            response = await asyncio.wait_for(future, timeout=timeout)
            if not response.get("success", False):
                raise RuntimeError(response.get("message", "Request failed"))
            return response.get("body", {})
        finally:
            self._pending.pop(seq, None)

    async def _read_loop(self) -> None:
        """Read and process messages from the debug adapter."""
        if not self._reader:
            return

        buffer = b""

        while True:
            try:
                data = await self._reader.read(4096)
                if not data:
                    break

                buffer += data

                # Process complete messages
                while True:
                    # Find Content-Length header
                    header_end = buffer.find(b"\r\n\r\n")
                    if header_end == -1:
                        break

                    header = buffer[:header_end].decode()
                    content_length = 0
                    for line in header.split("\r\n"):
                        if line.startswith("Content-Length:"):
                            content_length = int(line.split(":")[1].strip())
                            break

                    if content_length == 0:
                        buffer = buffer[header_end + 4:]
                        continue

                    content_start = header_end + 4
                    content_end = content_start + content_length

                    if len(buffer) < content_end:
                        break  # Need more data

                    content = buffer[content_start:content_end].decode()
                    buffer = buffer[content_end:]

                    # Parse and handle message
                    try:
                        message = json.loads(content)
                        await self._handle_message(message)
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid JSON from debug adapter: {content[:100]}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error reading from debug adapter: {e}")
                break

    async def _handle_message(self, message: dict) -> None:
        """Handle a received DAP message.

        Args:
            message: Parsed DAP message
        """
        msg_type = message.get("type")

        if msg_type == "response":
            seq = message.get("request_seq")
            if seq in self._pending:
                self._pending[seq].set_result(message)

        elif msg_type == "event":
            event_type = message.get("event")
            body = message.get("body", {})
            logger.debug(f"DAP event: {event_type}")

            handlers = self._event_handlers.get(event_type, [])
            for handler in handlers:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(body)
                    else:
                        handler(body)
                except Exception as e:
                    logger.error(f"Event handler error: {e}")


class PythonDebugger:
    """Python debugger using debugpy."""

    def __init__(self):
        """Initialize Python debugger."""
        self._process: Optional[subprocess.Popen] = None
        self._client: Optional[DAPClient] = None
        self._port: int = 0

    def _find_free_port(self) -> int:
        """Find an available port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    async def start(
        self,
        script_path: str,
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> bool:
        """Start a Python debug session.

        Args:
            script_path: Path to Python script
            args: Script arguments
            cwd: Working directory
            env: Environment variables

        Returns:
            True if started successfully
        """
        self._port = self._find_free_port()

        # Build command
        cmd = [
            "python", "-m", "debugpy",
            "--listen", f"127.0.0.1:{self._port}",
            "--wait-for-client",
            script_path,
        ]
        if args:
            cmd.extend(args)

        # Start process
        process_env = os.environ.copy()
        if env:
            process_env.update(env)

        try:
            self._process = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=process_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info(f"Started debugpy on port {self._port}")

            # Wait a bit for debugpy to start
            await asyncio.sleep(0.5)

            # Connect DAP client
            self._client = DAPClient(port=self._port)
            if not await self._client.connect():
                await self.stop()
                return False

            # Initialize
            await self._client.send_request("initialize", {
                "clientID": "glock",
                "clientName": "Glock CLI",
                "adapterID": "debugpy",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariableType": True,
                "supportsRunInTerminalRequest": False,
            })

            # Attach to debugpy
            await self._client.send_request("attach", {
                "justMyCode": False,
            })

            return True

        except FileNotFoundError:
            logger.error("debugpy not found. Install with: pip install debugpy")
            return False
        except Exception as e:
            logger.error(f"Failed to start Python debugger: {e}")
            await self.stop()
            return False

    async def stop(self) -> None:
        """Stop the debug session."""
        if self._client:
            try:
                await self._client.send_request("disconnect", {"terminateDebuggee": True})
            except Exception:
                pass
            await self._client.disconnect()
            self._client = None

        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    async def set_breakpoint(
        self,
        file_path: str,
        line: int,
        condition: Optional[str] = None,
    ) -> Optional[Breakpoint]:
        """Set a breakpoint.

        Args:
            file_path: Absolute file path
            line: Line number
            condition: Optional condition expression

        Returns:
            Breakpoint if set successfully
        """
        if not self._client:
            return None

        bp = {"line": line}
        if condition:
            bp["condition"] = condition

        response = await self._client.send_request("setBreakpoints", {
            "source": {"path": file_path},
            "breakpoints": [bp],
        })

        breakpoints = response.get("breakpoints", [])
        if breakpoints:
            bp_data = breakpoints[0]
            return Breakpoint(
                id=bp_data.get("id", 0),
                file_path=file_path,
                line=bp_data.get("line", line),
                condition=condition,
                verified=bp_data.get("verified", False),
            )
        return None

    async def continue_execution(self, thread_id: int = 1) -> None:
        """Continue execution.

        Args:
            thread_id: Thread to continue
        """
        if self._client:
            await self._client.send_request("continue", {"threadId": thread_id})

    async def step_over(self, thread_id: int = 1) -> None:
        """Step over (next line)."""
        if self._client:
            await self._client.send_request("next", {"threadId": thread_id})

    async def step_into(self, thread_id: int = 1) -> None:
        """Step into function."""
        if self._client:
            await self._client.send_request("stepIn", {"threadId": thread_id})

    async def step_out(self, thread_id: int = 1) -> None:
        """Step out of function."""
        if self._client:
            await self._client.send_request("stepOut", {"threadId": thread_id})

    async def get_stack_trace(self, thread_id: int = 1) -> list[StackFrame]:
        """Get current stack trace.

        Args:
            thread_id: Thread to inspect

        Returns:
            List of stack frames
        """
        if not self._client:
            return []

        response = await self._client.send_request("stackTrace", {
            "threadId": thread_id,
        })

        frames = []
        for frame_data in response.get("stackFrames", []):
            source = frame_data.get("source", {})
            frames.append(StackFrame(
                id=frame_data.get("id", 0),
                name=frame_data.get("name", ""),
                file_path=source.get("path", ""),
                line=frame_data.get("line", 0),
                column=frame_data.get("column", 0),
            ))
        return frames

    async def get_variables(self, scope_ref: int) -> list[Variable]:
        """Get variables in a scope.

        Args:
            scope_ref: Variables reference from scope

        Returns:
            List of variables
        """
        if not self._client:
            return []

        response = await self._client.send_request("variables", {
            "variablesReference": scope_ref,
        })

        variables = []
        for var_data in response.get("variables", []):
            variables.append(Variable(
                name=var_data.get("name", ""),
                value=var_data.get("value", ""),
                type=var_data.get("type", ""),
                children_ref=var_data.get("variablesReference", 0),
                is_expandable=var_data.get("variablesReference", 0) > 0,
            ))
        return variables

    async def evaluate(self, expression: str, frame_id: Optional[int] = None) -> str:
        """Evaluate an expression.

        Args:
            expression: Expression to evaluate
            frame_id: Stack frame context

        Returns:
            Result string
        """
        if not self._client:
            return "Not connected"

        args: dict[str, Any] = {
            "expression": expression,
            "context": "repl",
        }
        if frame_id is not None:
            args["frameId"] = frame_id

        response = await self._client.send_request("evaluate", args)
        return response.get("result", "")

    def on_event(self, event_type: str, handler: Callable) -> None:
        """Register an event handler.

        Args:
            event_type: Event type (stopped, output, etc.)
            handler: Callback function
        """
        if self._client:
            self._client.on_event(event_type, handler)


class NodeDebugger:
    """Node.js debugger using Chrome DevTools Protocol."""

    def __init__(self):
        """Initialize Node.js debugger."""
        self._process: Optional[subprocess.Popen] = None
        self._port: int = 9229

    def _find_free_port(self) -> int:
        """Find an available port."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    async def start(
        self,
        script_path: str,
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> bool:
        """Start a Node.js debug session.

        Args:
            script_path: Path to JavaScript file
            args: Script arguments
            cwd: Working directory
            env: Environment variables

        Returns:
            True if started successfully
        """
        self._port = self._find_free_port()

        # Build command
        cmd = [
            "node",
            f"--inspect-brk=127.0.0.1:{self._port}",
            script_path,
        ]
        if args:
            cmd.extend(args)

        # Start process
        process_env = os.environ.copy()
        if env:
            process_env.update(env)

        try:
            self._process = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=process_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            logger.info(f"Started node --inspect on port {self._port}")

            # Wait for debugger to be ready
            await asyncio.sleep(1.0)

            return True

        except FileNotFoundError:
            logger.error("Node.js not found")
            return False
        except Exception as e:
            logger.error(f"Failed to start Node.js debugger: {e}")
            await self.stop()
            return False

    async def stop(self) -> None:
        """Stop the debug session."""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    @property
    def debug_url(self) -> str:
        """Get the Chrome DevTools debug URL."""
        return f"chrome://inspect/#devices"


class DebugManager:
    """High-level debug manager.

    Coordinates debugging across multiple languages.
    """

    def __init__(self):
        """Initialize debug manager."""
        self._sessions: dict[str, Any] = {}
        self._current_session: Optional[str] = None

    async def start_session(
        self,
        session_id: str,
        language: str,
        script_path: str,
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
    ) -> bool:
        """Start a new debug session.

        Args:
            session_id: Unique session identifier
            language: "python" or "node"
            script_path: Path to script
            args: Script arguments
            cwd: Working directory

        Returns:
            True if started successfully
        """
        if session_id in self._sessions:
            await self.stop_session(session_id)

        if language == "python":
            debugger = PythonDebugger()
        elif language in ("node", "nodejs", "javascript"):
            debugger = NodeDebugger()
        else:
            logger.error(f"Unsupported language: {language}")
            return False

        if await debugger.start(script_path, args, cwd):
            self._sessions[session_id] = debugger
            self._current_session = session_id
            return True
        return False

    async def stop_session(self, session_id: Optional[str] = None) -> None:
        """Stop a debug session.

        Args:
            session_id: Session to stop (current if None)
        """
        session_id = session_id or self._current_session
        if session_id and session_id in self._sessions:
            await self._sessions[session_id].stop()
            del self._sessions[session_id]
            if self._current_session == session_id:
                self._current_session = None

    def get_session(self, session_id: Optional[str] = None):
        """Get a debug session.

        Args:
            session_id: Session ID (current if None)

        Returns:
            Debugger instance or None
        """
        session_id = session_id or self._current_session
        return self._sessions.get(session_id)

    async def stop_all(self) -> None:
        """Stop all debug sessions."""
        for session_id in list(self._sessions.keys()):
            await self.stop_session(session_id)


# Tool handlers for integration with tool broker
async def debug_start_handler(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Start a debug session.

    Args:
        args: {language, script_path, args?, cwd?}
        context: Execution context

    Returns:
        Result with session info
    """
    manager = context.get("debug_manager")
    if not manager:
        manager = DebugManager()
        context["debug_manager"] = manager

    language = args.get("language", "python")
    script_path = args.get("script_path")
    script_args = args.get("args", [])
    cwd = args.get("cwd") or context.get("workspace_dir")

    if not script_path:
        return {"error": "script_path required"}

    session_id = f"debug_{language}_{Path(script_path).stem}"

    if await manager.start_session(session_id, language, script_path, script_args, cwd):
        return {
            "session_id": session_id,
            "language": language,
            "status": "started",
            "message": f"Debug session started for {script_path}",
        }
    return {"error": f"Failed to start debug session for {language}"}


async def debug_breakpoint_handler(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Set a breakpoint.

    Args:
        args: {file_path, line, condition?}
        context: Execution context

    Returns:
        Result with breakpoint info
    """
    manager = context.get("debug_manager")
    if not manager:
        return {"error": "No debug session active"}

    debugger = manager.get_session()
    if not debugger or not isinstance(debugger, PythonDebugger):
        return {"error": "No Python debug session active"}

    file_path = args.get("file_path")
    line = args.get("line")
    condition = args.get("condition")

    if not file_path or not line:
        return {"error": "file_path and line required"}

    bp = await debugger.set_breakpoint(file_path, line, condition)
    if bp:
        return {
            "breakpoint_id": bp.id,
            "file": bp.file_path,
            "line": bp.line,
            "verified": bp.verified,
        }
    return {"error": "Failed to set breakpoint"}


async def debug_continue_handler(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Continue execution.

    Args:
        args: {action: continue|step_over|step_into|step_out}
        context: Execution context

    Returns:
        Result with status
    """
    manager = context.get("debug_manager")
    if not manager:
        return {"error": "No debug session active"}

    debugger = manager.get_session()
    if not debugger or not isinstance(debugger, PythonDebugger):
        return {"error": "No Python debug session active"}

    action = args.get("action", "continue")

    if action == "continue":
        await debugger.continue_execution()
    elif action == "step_over":
        await debugger.step_over()
    elif action == "step_into":
        await debugger.step_into()
    elif action == "step_out":
        await debugger.step_out()
    else:
        return {"error": f"Unknown action: {action}"}

    return {"status": "ok", "action": action}


async def debug_stack_handler(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Get stack trace.

    Args:
        args: {}
        context: Execution context

    Returns:
        Stack trace info
    """
    manager = context.get("debug_manager")
    if not manager:
        return {"error": "No debug session active"}

    debugger = manager.get_session()
    if not debugger or not isinstance(debugger, PythonDebugger):
        return {"error": "No Python debug session active"}

    frames = await debugger.get_stack_trace()
    return {
        "frames": [
            {
                "id": f.id,
                "name": f.name,
                "file": f.file_path,
                "line": f.line,
            }
            for f in frames
        ]
    }


async def debug_evaluate_handler(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Evaluate an expression.

    Args:
        args: {expression, frame_id?}
        context: Execution context

    Returns:
        Evaluation result
    """
    manager = context.get("debug_manager")
    if not manager:
        return {"error": "No debug session active"}

    debugger = manager.get_session()
    if not debugger or not isinstance(debugger, PythonDebugger):
        return {"error": "No Python debug session active"}

    expression = args.get("expression")
    frame_id = args.get("frame_id")

    if not expression:
        return {"error": "expression required"}

    result = await debugger.evaluate(expression, frame_id)
    return {"expression": expression, "result": result}


async def debug_stop_handler(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Stop debug session.

    Args:
        args: {session_id?}
        context: Execution context

    Returns:
        Status
    """
    manager = context.get("debug_manager")
    if not manager:
        return {"error": "No debug session active"}

    session_id = args.get("session_id")
    await manager.stop_session(session_id)

    return {"status": "stopped"}
