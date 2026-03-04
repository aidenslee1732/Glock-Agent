"""Council Integration for Model B.

Bridges the OrchestrationEngine with the Council system to provide
automated code review before executing write operations.

The council evaluates proposed code changes from multiple perspectives:
- Correctness
- Security
- Simplicity
- Edge cases
- And more based on configuration

This ensures code quality before changes are made.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import ModeManager

logger = logging.getLogger(__name__)


# Language detection patterns
LANGUAGE_PATTERNS = {
    ".py": "python",
    ".pyw": "python",
    ".pyx": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".sql": "sql",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "zsh",
}


def detect_language(file_path: str) -> str:
    """Detect programming language from file extension."""
    path = Path(file_path)
    suffix = path.suffix.lower()
    return LANGUAGE_PATTERNS.get(suffix, "unknown")


@dataclass
class CouncilResult:
    """Result from council evaluation."""
    approved: bool
    confidence: float
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    reasoning: str = ""
    execution_time_ms: int = 0
    perspectives_completed: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_feedback(self) -> str:
        """Convert to feedback string for LLM context."""
        parts = []

        if not self.approved:
            parts.append(f"**COUNCIL REJECTED** (confidence: {self.confidence:.0%})")
            parts.append("")
            parts.append("## Issues Found")
            for issue in self.issues:
                parts.append(f"- {issue}")

            if self.suggestions:
                parts.append("")
                parts.append("## Suggestions")
                for suggestion in self.suggestions:
                    parts.append(f"- {suggestion}")

            if self.reasoning:
                parts.append("")
                parts.append("## Details")
                # Truncate long reasoning
                if len(self.reasoning) > 1000:
                    parts.append(self.reasoning[:1000] + "...")
                else:
                    parts.append(self.reasoning)
        else:
            parts.append(f"**COUNCIL APPROVED** (confidence: {self.confidence:.0%})")
            if self.suggestions:
                parts.append("")
                parts.append("## Recommendations")
                for suggestion in self.suggestions[:3]:  # Limit to top 3
                    parts.append(f"- {suggestion}")

        return "\n".join(parts)


# Type for LLM callback
LLMCallback = Callable[[str, str, str], Coroutine[Any, Any, str]]


class CouncilIntegration:
    """Bridge between OrchestrationEngine and Council system.

    Evaluates proposed code changes before execution by:
    1. Checking if council review is needed based on mode
    2. Running council deliberation if needed
    3. Returning approval/rejection with feedback

    Usage in engine:
        if tool_name in ("edit_file", "write_file"):
            result = await council.evaluate_proposed_change(
                task=current_task,
                proposed_code=args["content"],
                file_path=args["file_path"],
                llm_callback=self.agent_llm_callback,
            )
            if not result.approved:
                # Add feedback to context, let LLM revise
    """

    def __init__(
        self,
        mode_manager: Optional["ModeManager"] = None,
        default_timeout: float = 120.0,
        skip_trivial_changes: bool = True,
        trivial_threshold: int = 10,  # Lines of code
    ):
        """Initialize council integration.

        Args:
            mode_manager: Mode manager for determining council settings
            default_timeout: Default timeout for council deliberation
            skip_trivial_changes: Skip council for trivial changes
            trivial_threshold: Max lines for trivial changes
        """
        self._mode_manager = mode_manager
        self._default_timeout = default_timeout
        self._skip_trivial = skip_trivial_changes
        self._trivial_threshold = trivial_threshold

        # Lazy-loaded council orchestrator
        self._orchestrator = None
        self._council_available = None

        # Track evaluation history
        self._evaluations: list[dict[str, Any]] = []

    @property
    def council_available(self) -> bool:
        """Check if council system is available."""
        if self._council_available is None:
            try:
                from apps.server.src.planner.council.orchestrator import (
                    CouncilOrchestrator,
                    CouncilConfig,
                )
                self._council_available = True
            except ImportError:
                logger.warning("Council system not available")
                self._council_available = False
        return self._council_available

    def _get_orchestrator(self):
        """Get or create council orchestrator."""
        if self._orchestrator is None and self.council_available:
            from apps.server.src.planner.council.orchestrator import (
                CouncilOrchestrator,
                CouncilConfig,
                CouncilStrategy,
            )

            # Get config from mode manager if available
            perspectives = ["correctness", "security", "simplicity", "edge_cases"]
            timeout = self._default_timeout

            if self._mode_manager:
                mode_config = self._mode_manager.config
                if hasattr(mode_config, "council_perspectives"):
                    perspectives = mode_config.council_perspectives
                if hasattr(mode_config, "council_timeout"):
                    timeout = mode_config.council_timeout

            config = CouncilConfig(
                perspectives=perspectives,
                strategy=CouncilStrategy.PARALLEL,
                enable_quality_gate=True,
                total_timeout=timeout,
            )

            self._orchestrator = CouncilOrchestrator(config=config)

        return self._orchestrator

    def should_review(
        self,
        task_complexity: str = "normal",
        code_size: int = 0,
    ) -> bool:
        """Determine if council review should run.

        Args:
            task_complexity: "trivial", "simple", "normal", "complex"
            code_size: Size of proposed change in characters

        Returns:
            Whether to run council review
        """
        # Check if council is available
        if not self.council_available:
            return False

        # Check mode settings
        if self._mode_manager:
            if not self._mode_manager.should_run_council(task_complexity):
                return False

        # Skip trivial changes if configured
        if self._skip_trivial:
            lines = code_size // 40  # Rough estimate of lines
            if lines < self._trivial_threshold and task_complexity == "trivial":
                return False

        return True

    def _estimate_complexity(
        self,
        proposed_code: str,
        task: str,
    ) -> str:
        """Estimate task/code complexity.

        Args:
            proposed_code: The code being proposed
            task: The task description

        Returns:
            Complexity level: "trivial", "simple", "normal", "complex"
        """
        lines = proposed_code.count("\n")

        # Check task indicators
        task_lower = task.lower()
        complex_indicators = ["security", "authentication", "database", "api", "encryption"]
        simple_indicators = ["fix typo", "rename", "comment", "formatting"]

        if any(ind in task_lower for ind in complex_indicators):
            return "complex"

        if any(ind in task_lower for ind in simple_indicators):
            return "trivial" if lines < 5 else "simple"

        # Based on code size
        if lines < 5:
            return "trivial"
        elif lines < 20:
            return "simple"
        elif lines < 100:
            return "normal"
        else:
            return "complex"

    async def evaluate_proposed_change(
        self,
        task: str,
        proposed_code: str,
        file_path: str,
        llm_callback: LLMCallback,
        existing_content: Optional[str] = None,
        force_review: bool = False,
    ) -> CouncilResult:
        """Evaluate a proposed code change with the council.

        Args:
            task: The task/goal being accomplished
            proposed_code: The code to be written/edited
            file_path: Target file path
            llm_callback: Async LLM callback (system, prompt, model_tier) -> response
            existing_content: Current file content (for edits)
            force_review: Force review even if skipped normally

        Returns:
            CouncilResult with approval status and feedback
        """
        # Check if we should skip review
        complexity = self._estimate_complexity(proposed_code, task)
        if not force_review and not self.should_review(complexity, len(proposed_code)):
            return CouncilResult(
                approved=True,
                confidence=1.0,
                metadata={"skipped": True, "reason": "trivial_change"},
            )

        # Get orchestrator
        orchestrator = self._get_orchestrator()
        if orchestrator is None:
            return CouncilResult(
                approved=True,
                confidence=0.5,
                metadata={"skipped": True, "reason": "council_unavailable"},
            )

        # Prepare context
        language = detect_language(file_path)
        context = {
            "language": language,
            "file_path": file_path,
            "existing_content": existing_content,
            "task_complexity": complexity,
        }

        # Run council deliberation
        try:
            result = await orchestrator.deliberate(
                task=task,
                proposed_code=proposed_code,
                context=context,
                llm_callback=llm_callback,
            )

            # Convert to our result format
            issues = []
            suggestions = []

            # Extract issues from consensus
            for issue in result.consensus.critical_issues:
                issues.append(f"[{issue.severity.value.upper()}] {issue.message}")

            for issue in result.consensus.all_issues:
                if issue.severity.value in ("error", "warning"):
                    issues.append(f"[{issue.severity.value.upper()}] {issue.message}")

            # Get recommendations
            suggestions = result.recommendations[:5]  # Top 5

            # Build reasoning summary
            reasoning_parts = []
            for pr in result.perspective_results[:3]:  # Top 3 perspectives
                status = "✓" if pr.approved else "✗"
                reasoning_parts.append(
                    f"**{pr.perspective_type.value}** {status}: {pr.reasoning[:200]}..."
                    if len(pr.reasoning) > 200 else
                    f"**{pr.perspective_type.value}** {status}: {pr.reasoning}"
                )

            council_result = CouncilResult(
                approved=result.approved,
                confidence=result.consensus.confidence,
                issues=issues,
                suggestions=suggestions,
                reasoning="\n\n".join(reasoning_parts),
                execution_time_ms=result.execution_time_ms,
                perspectives_completed=result.perspectives_completed,
                metadata={
                    "quality_score": result.quality_score.overall if result.quality_score else None,
                    "quality_level": result.quality_score.level.value if result.quality_score else None,
                    "perspectives_failed": result.perspectives_failed,
                },
            )

            # Track evaluation
            self._evaluations.append({
                "file_path": file_path,
                "approved": result.approved,
                "complexity": complexity,
                "execution_time_ms": result.execution_time_ms,
            })

            return council_result

        except asyncio.TimeoutError:
            logger.warning(f"Council evaluation timed out for {file_path}")
            # Bug fix 1.4: Return approved=False on timeout for safety
            return CouncilResult(
                approved=False,
                confidence=0.0,
                issues=["Council evaluation timed out - review required before proceeding"],
                suggestions=["Retry the operation or manually review the proposed changes"],
                metadata={"skipped": True, "reason": "timeout"},
            )

        except Exception as e:
            logger.error(f"Council evaluation failed: {e}")
            # Bug fix 1.4: Return approved=False on error for safety
            return CouncilResult(
                approved=False,
                confidence=0.0,
                issues=[f"Council evaluation failed: {str(e)[:100]}"],
                suggestions=["Check the council configuration and retry"],
                metadata={"skipped": True, "reason": f"error: {e}"},
            )

    async def evaluate_edit(
        self,
        task: str,
        file_path: str,
        old_string: str,
        new_string: str,
        llm_callback: LLMCallback,
        full_content: Optional[str] = None,
    ) -> CouncilResult:
        """Evaluate an edit_file operation.

        Creates a diff-style representation for council review.

        Args:
            task: The task being accomplished
            file_path: File being edited
            old_string: String being replaced
            new_string: Replacement string
            llm_callback: LLM callback
            full_content: Full file content for context

        Returns:
            CouncilResult
        """
        # Build diff representation
        diff_code = f"""# Edit in {file_path}
# OLD:
{old_string}

# NEW:
{new_string}
"""
        return await self.evaluate_proposed_change(
            task=task,
            proposed_code=diff_code,
            file_path=file_path,
            llm_callback=llm_callback,
            existing_content=full_content,
        )

    def get_evaluation_stats(self) -> dict[str, Any]:
        """Get statistics about council evaluations."""
        if not self._evaluations:
            return {"total": 0}

        total = len(self._evaluations)
        approved = sum(1 for e in self._evaluations if e["approved"])
        avg_time = sum(e["execution_time_ms"] for e in self._evaluations) / total

        by_complexity = {}
        for e in self._evaluations:
            c = e["complexity"]
            if c not in by_complexity:
                by_complexity[c] = {"total": 0, "approved": 0}
            by_complexity[c]["total"] += 1
            if e["approved"]:
                by_complexity[c]["approved"] += 1

        return {
            "total": total,
            "approved": approved,
            "rejected": total - approved,
            "approval_rate": approved / total,
            "avg_execution_time_ms": avg_time,
            "by_complexity": by_complexity,
        }

    def clear_history(self) -> None:
        """Clear evaluation history."""
        self._evaluations = []
