"""Performance profiling tools for Glock CLI.

Provides profiling integration for:
- Python profiling via cProfile and py-spy
- Node.js profiling via built-in profiler
- Flame graph generation
- Performance analysis and suggestions

Usage:
    profiler = ProfilerManager()
    result = await profiler.profile_python("script.py")
    flame_graph = await profiler.generate_flame_graph(result)
"""

from __future__ import annotations

import asyncio
import cProfile
import io
import json
import logging
import os
import pstats
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ProfileType(Enum):
    """Types of profiling."""
    CPU = "cpu"
    MEMORY = "memory"
    TIME = "time"


@dataclass
class FunctionStats:
    """Statistics for a single function.

    Attributes:
        name: Function name
        filename: Source file
        line: Line number
        calls: Number of calls
        total_time: Total time in function
        cumulative_time: Cumulative time including children
        avg_time_per_call: Average time per call
    """
    name: str
    filename: str
    line: int
    calls: int
    total_time: float
    cumulative_time: float
    avg_time_per_call: float

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "name": self.name,
            "filename": self.filename,
            "line": self.line,
            "calls": self.calls,
            "total_time_ms": self.total_time * 1000,
            "cumulative_time_ms": self.cumulative_time * 1000,
            "avg_time_per_call_ms": self.avg_time_per_call * 1000,
        }


@dataclass
class ProfileResult:
    """Result of a profiling run.

    Attributes:
        profile_type: Type of profiling performed
        target: Script or function profiled
        duration: Total profiling duration
        functions: List of function statistics
        hotspots: Top functions by time
        suggestions: Performance improvement suggestions
        raw_data: Raw profiler output
    """
    profile_type: ProfileType
    target: str
    duration: float
    functions: list[FunctionStats] = field(default_factory=list)
    hotspots: list[FunctionStats] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    raw_data: Optional[bytes] = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "profile_type": self.profile_type.value,
            "target": self.target,
            "duration_ms": self.duration * 1000,
            "hotspots": [f.to_dict() for f in self.hotspots[:10]],
            "suggestions": self.suggestions,
            "timestamp": self.timestamp.isoformat(),
        }

    def summary(self) -> str:
        """Get a human-readable summary."""
        lines = [
            f"Profile: {self.target}",
            f"Duration: {self.duration * 1000:.2f}ms",
            f"Functions: {len(self.functions)}",
            "",
            "Top 5 Hotspots:",
        ]

        for i, func in enumerate(self.hotspots[:5], 1):
            lines.append(
                f"  {i}. {func.name} ({func.filename}:{func.line}) - "
                f"{func.cumulative_time * 1000:.2f}ms ({func.calls} calls)"
            )

        if self.suggestions:
            lines.append("")
            lines.append("Suggestions:")
            for suggestion in self.suggestions[:5]:
                lines.append(f"  - {suggestion}")

        return "\n".join(lines)


class PythonProfiler:
    """Python profiler using cProfile."""

    def __init__(self):
        """Initialize Python profiler."""
        self._profiler: Optional[cProfile.Profile] = None

    def profile_script(
        self,
        script_path: str,
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
    ) -> ProfileResult:
        """Profile a Python script.

        Args:
            script_path: Path to Python script
            args: Script arguments
            cwd: Working directory

        Returns:
            ProfileResult with statistics
        """
        import sys
        import time

        script_path = str(Path(script_path).resolve())

        # Prepare sys.argv
        old_argv = sys.argv.copy()
        sys.argv = [script_path] + (args or [])

        # Change directory if needed
        old_cwd = os.getcwd()
        if cwd:
            os.chdir(cwd)

        try:
            # Run profiler
            profiler = cProfile.Profile()
            start_time = time.time()

            try:
                profiler.run(f"exec(open('{script_path}').read())")
            except SystemExit:
                pass  # Script called sys.exit()
            except Exception as e:
                logger.warning(f"Script raised exception: {e}")

            duration = time.time() - start_time

            # Get stats
            stats_stream = io.StringIO()
            stats = pstats.Stats(profiler, stream=stats_stream)
            stats.sort_stats('cumulative')

            # Parse stats
            functions = self._parse_stats(stats)
            hotspots = sorted(functions, key=lambda f: f.cumulative_time, reverse=True)[:20]

            # Generate suggestions
            suggestions = self._generate_suggestions(functions)

            return ProfileResult(
                profile_type=ProfileType.CPU,
                target=script_path,
                duration=duration,
                functions=functions,
                hotspots=hotspots,
                suggestions=suggestions,
            )

        finally:
            sys.argv = old_argv
            os.chdir(old_cwd)

    def _parse_stats(self, stats: pstats.Stats) -> list[FunctionStats]:
        """Parse pstats into FunctionStats.

        Args:
            stats: pstats.Stats object

        Returns:
            List of FunctionStats
        """
        functions = []

        for (filename, line, name), (ncalls, totcalls, tottime, cumtime, callers) in stats.stats.items():
            # Skip internal profiler functions
            if "cProfile" in filename or "pstats" in filename:
                continue

            avg_time = tottime / ncalls if ncalls > 0 else 0

            functions.append(FunctionStats(
                name=name,
                filename=filename,
                line=line,
                calls=ncalls,
                total_time=tottime,
                cumulative_time=cumtime,
                avg_time_per_call=avg_time,
            ))

        return functions

    def _generate_suggestions(self, functions: list[FunctionStats]) -> list[str]:
        """Generate performance suggestions based on profile.

        Args:
            functions: List of function statistics

        Returns:
            List of suggestion strings
        """
        suggestions = []

        # Find hotspots
        sorted_funcs = sorted(functions, key=lambda f: f.cumulative_time, reverse=True)

        if sorted_funcs:
            top_func = sorted_funcs[0]
            if top_func.cumulative_time > 1.0:  # More than 1 second
                suggestions.append(
                    f"Function '{top_func.name}' takes {top_func.cumulative_time:.2f}s - "
                    "consider optimization or caching"
                )

        # Find frequently called functions
        frequent_calls = [f for f in functions if f.calls > 10000]
        for func in frequent_calls[:3]:
            suggestions.append(
                f"Function '{func.name}' called {func.calls:,} times - "
                "consider memoization or reducing call frequency"
            )

        # Find slow average time
        slow_avg = [f for f in functions if f.avg_time_per_call > 0.01 and f.calls > 100]
        for func in slow_avg[:2]:
            suggestions.append(
                f"Function '{func.name}' averages {func.avg_time_per_call * 1000:.2f}ms per call - "
                "consider optimization"
            )

        # Check for known slow patterns
        for func in functions:
            if "json.loads" in func.name or "json.dumps" in func.name:
                if func.calls > 1000:
                    suggestions.append(
                        f"Frequent JSON parsing ({func.calls} calls) - consider orjson for faster JSON"
                    )

            if "re.compile" in func.name:
                if func.calls > 10:
                    suggestions.append(
                        "Multiple regex compilations detected - compile patterns once at module level"
                    )

            if "open" in func.name and func.calls > 100:
                suggestions.append(
                    f"Frequent file opens ({func.calls} times) - consider batching file operations"
                )

        return suggestions


class PySpy:
    """Python profiler using py-spy for sampling profiling."""

    @staticmethod
    def is_available() -> bool:
        """Check if py-spy is installed."""
        try:
            result = subprocess.run(
                ["py-spy", "--version"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    async def profile_script(
        self,
        script_path: str,
        duration: int = 30,
        output_format: str = "speedscope",
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
    ) -> Optional[Path]:
        """Profile a script with py-spy.

        Args:
            script_path: Path to Python script
            duration: Maximum profiling duration in seconds
            output_format: Output format (speedscope, flamegraph, raw)
            args: Script arguments
            cwd: Working directory

        Returns:
            Path to output file or None
        """
        if not self.is_available():
            logger.error("py-spy not installed. Install with: pip install py-spy")
            return None

        # Create output file
        suffix = ".json" if output_format == "speedscope" else ".svg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            output_path = Path(f.name)

        # Build command
        cmd = [
            "py-spy", "record",
            "-o", str(output_path),
            "-d", str(duration),
            "-f", output_format,
            "--", "python", script_path,
        ]
        if args:
            cmd.extend(args)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                return output_path
            else:
                logger.error(f"py-spy failed: {stderr.decode()}")
                output_path.unlink(missing_ok=True)
                return None

        except Exception as e:
            logger.error(f"py-spy profiling failed: {e}")
            output_path.unlink(missing_ok=True)
            return None

    async def attach_to_process(
        self,
        pid: int,
        duration: int = 30,
        output_format: str = "flamegraph",
    ) -> Optional[Path]:
        """Attach py-spy to a running process.

        Args:
            pid: Process ID to profile
            duration: Profiling duration
            output_format: Output format

        Returns:
            Path to output file
        """
        if not self.is_available():
            return None

        suffix = ".json" if output_format == "speedscope" else ".svg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            output_path = Path(f.name)

        cmd = [
            "py-spy", "record",
            "-o", str(output_path),
            "-d", str(duration),
            "-f", output_format,
            "--pid", str(pid),
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                return output_path
            else:
                logger.error(f"py-spy attach failed: {stderr.decode()}")
                output_path.unlink(missing_ok=True)
                return None

        except Exception as e:
            logger.error(f"py-spy attach failed: {e}")
            output_path.unlink(missing_ok=True)
            return None


class NodeProfiler:
    """Node.js profiler using V8 profiler."""

    async def profile_script(
        self,
        script_path: str,
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
    ) -> Optional[Path]:
        """Profile a Node.js script.

        Args:
            script_path: Path to JavaScript file
            args: Script arguments
            cwd: Working directory

        Returns:
            Path to profile output
        """
        # Create temp directory for profile
        with tempfile.TemporaryDirectory() as temp_dir:
            profile_dir = Path(temp_dir)

            cmd = [
                "node",
                "--prof",
                script_path,
            ]
            if args:
                cmd.extend(args)

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=cwd or str(profile_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                stdout, stderr = await process.communicate()

                # Find the isolate log file
                log_files = list(profile_dir.glob("isolate-*.log"))
                if not log_files:
                    cwd_path = Path(cwd) if cwd else Path.cwd()
                    log_files = list(cwd_path.glob("isolate-*.log"))

                if log_files:
                    # Process the log file
                    log_file = log_files[0]
                    output_path = log_file.with_suffix(".processed.txt")

                    process_cmd = [
                        "node",
                        "--prof-process",
                        str(log_file),
                    ]

                    result = await asyncio.create_subprocess_exec(
                        *process_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )

                    stdout, stderr = await result.communicate()

                    if result.returncode == 0:
                        output_path.write_bytes(stdout)
                        return output_path

                logger.warning("No Node.js profile output found")
                return None

            except FileNotFoundError:
                logger.error("Node.js not found")
                return None
            except Exception as e:
                logger.error(f"Node.js profiling failed: {e}")
                return None


class FlameGraphGenerator:
    """Generate flame graphs from profile data."""

    @staticmethod
    def from_cprofile(result: ProfileResult) -> Optional[str]:
        """Generate a text-based flame graph representation.

        Args:
            result: ProfileResult from cProfile

        Returns:
            ASCII flame graph or None
        """
        if not result.functions:
            return None

        lines = ["Flame Graph (text representation):", "=" * 60]

        # Group by time
        max_time = max(f.cumulative_time for f in result.functions) if result.functions else 1

        for func in result.hotspots[:15]:
            bar_width = int((func.cumulative_time / max_time) * 50)
            bar = "█" * bar_width + "░" * (50 - bar_width)
            time_ms = func.cumulative_time * 1000
            lines.append(f"{bar} {time_ms:>8.2f}ms {func.name[:30]}")

        return "\n".join(lines)


class ProfilerManager:
    """High-level profiler manager."""

    def __init__(self):
        """Initialize profiler manager."""
        self._python_profiler = PythonProfiler()
        self._pyspy = PySpy()
        self._node_profiler = NodeProfiler()

    def profile_python_sync(
        self,
        script_path: str,
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
    ) -> ProfileResult:
        """Profile a Python script synchronously.

        Args:
            script_path: Path to script
            args: Script arguments
            cwd: Working directory

        Returns:
            ProfileResult with statistics
        """
        return self._python_profiler.profile_script(script_path, args, cwd)

    async def profile_python_sampling(
        self,
        script_path: str,
        duration: int = 30,
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
    ) -> Optional[Path]:
        """Profile Python using sampling (py-spy).

        Args:
            script_path: Path to script
            duration: Max duration in seconds
            args: Script arguments
            cwd: Working directory

        Returns:
            Path to flame graph SVG
        """
        return await self._pyspy.profile_script(
            script_path,
            duration,
            output_format="flamegraph",
            args=args,
            cwd=cwd,
        )

    async def profile_node(
        self,
        script_path: str,
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
    ) -> Optional[Path]:
        """Profile a Node.js script.

        Args:
            script_path: Path to JavaScript file
            args: Script arguments
            cwd: Working directory

        Returns:
            Path to profile output
        """
        return await self._node_profiler.profile_script(script_path, args, cwd)

    def generate_flame_graph(self, result: ProfileResult) -> Optional[str]:
        """Generate a flame graph representation.

        Args:
            result: Profile result

        Returns:
            Text flame graph
        """
        return FlameGraphGenerator.from_cprofile(result)


# Tool handlers for integration with tool broker
async def profile_python_handler(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Profile a Python script.

    Args:
        args: {script_path, args?, cwd?, sampling?}
        context: Execution context

    Returns:
        Profile result
    """
    script_path = args.get("script_path")
    if not script_path:
        return {"error": "script_path required"}

    script_args = args.get("args", [])
    cwd = args.get("cwd") or context.get("workspace_dir")
    use_sampling = args.get("sampling", False)

    manager = ProfilerManager()

    if use_sampling:
        if not PySpy.is_available():
            return {"error": "py-spy not installed. Install with: pip install py-spy"}

        duration = args.get("duration", 30)
        output_path = await manager.profile_python_sampling(
            script_path, duration, script_args, cwd
        )

        if output_path:
            return {
                "output_file": str(output_path),
                "format": "flamegraph",
                "message": f"Flame graph saved to {output_path}",
            }
        return {"error": "Sampling profiling failed"}

    else:
        result = manager.profile_python_sync(script_path, script_args, cwd)
        return {
            "summary": result.summary(),
            "hotspots": [f.to_dict() for f in result.hotspots[:10]],
            "suggestions": result.suggestions,
            "duration_ms": result.duration * 1000,
        }


async def profile_node_handler(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Profile a Node.js script.

    Args:
        args: {script_path, args?, cwd?}
        context: Execution context

    Returns:
        Profile result
    """
    script_path = args.get("script_path")
    if not script_path:
        return {"error": "script_path required"}

    script_args = args.get("args", [])
    cwd = args.get("cwd") or context.get("workspace_dir")

    manager = ProfilerManager()
    output_path = await manager.profile_node(script_path, script_args, cwd)

    if output_path:
        return {
            "output_file": str(output_path),
            "message": f"Profile saved to {output_path}",
        }
    return {"error": "Node.js profiling failed"}


async def profile_analyze_handler(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Analyze a profile result file.

    Args:
        args: {profile_path}
        context: Execution context

    Returns:
        Analysis results
    """
    profile_path = args.get("profile_path")
    if not profile_path:
        return {"error": "profile_path required"}

    path = Path(profile_path)
    if not path.exists():
        return {"error": f"Profile not found: {profile_path}"}

    try:
        content = path.read_text()

        # Detect format and parse
        if path.suffix == ".json":
            # Speedscope format
            data = json.loads(content)
            return {
                "format": "speedscope",
                "profiles": len(data.get("profiles", [])),
                "shared": len(data.get("shared", {}).get("frames", [])),
            }

        # Text format from node --prof-process
        if "Statistical profiling" in content:
            return {
                "format": "v8",
                "content_preview": content[:2000],
            }

        return {
            "format": "unknown",
            "size_bytes": len(content),
        }

    except Exception as e:
        return {"error": f"Failed to analyze profile: {e}"}
