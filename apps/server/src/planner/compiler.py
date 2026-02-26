"""Plan compiler - compiles tasks into signed execution plans."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from packages.shared_protocol.types import (
    CompiledPlan,
    PlanBudgets,
    ApprovalRule,
    generate_plan_id,
)

from .analyzer import TaskAnalyzer, TaskAnalysis, RiskLevel, Complexity, TaskType
from .signer import PlanSigner, SignatureInfo

logger = logging.getLogger(__name__)


# Default budgets by complexity
DEFAULT_BUDGETS = {
    Complexity.TRIVIAL: PlanBudgets(
        max_iterations=10,
        max_tool_calls=20,
        max_retries=1,
        timeout_ms=120000,
    ),
    Complexity.SIMPLE: PlanBudgets(
        max_iterations=20,
        max_tool_calls=50,
        max_retries=2,
        timeout_ms=300000,
    ),
    Complexity.MODERATE: PlanBudgets(
        max_iterations=50,
        max_tool_calls=100,
        max_retries=2,
        timeout_ms=600000,
    ),
    Complexity.COMPLEX: PlanBudgets(
        max_iterations=100,
        max_tool_calls=200,
        max_retries=3,
        timeout_ms=1200000,
    ),
    Complexity.CRITICAL: PlanBudgets(
        max_iterations=150,
        max_tool_calls=300,
        max_retries=3,
        timeout_ms=1800000,
    ),
}

# Tool approval rules by risk
TOOL_APPROVAL_RULES = {
    "bash": ApprovalRule(
        patterns=["rm", "sudo", "chmod", "chown", "kill", "pkill", "dd", "mkfs"],
        require_approval=True,
    ),
    "edit_file": ApprovalRule(
        patterns=[".env", "secret", "credentials", "password"],
        require_approval=True,
    ),
    "write_file": ApprovalRule(
        patterns=[".env", "secret", "credentials", "password"],
        require_approval=True,
    ),
}

# Plan validity duration
PLAN_VALIDITY_HOURS = 1


@dataclass
class CompilationContext:
    """Context for plan compilation."""
    session_id: str
    task_id: str
    user_id: str
    user_prompt: str
    workspace_scope: Optional[str] = None
    active_files: list[str] = None
    git_status: Optional[dict[str, Any]] = None
    available_validations: list[str] = None
    user_preferences: Optional[dict[str, Any]] = None

    def __post_init__(self):
        self.active_files = self.active_files or []
        self.available_validations = self.available_validations or []


class PlanCompiler:
    """Compiles tasks into signed execution plans.

    The plan compiler:
    1. Analyzes the task for type, complexity, and risk
    2. Determines allowed tools and approval requirements
    3. Sets validation steps and budgets
    4. Signs the plan for tamper-proof delivery to client
    """

    def __init__(
        self,
        analyzer: Optional[TaskAnalyzer] = None,
        signer: Optional[PlanSigner] = None,
    ):
        self.analyzer = analyzer or TaskAnalyzer()
        self.signer = signer or PlanSigner.from_env()

    def compile(
        self,
        context: CompilationContext,
        mode: str = "direct",
    ) -> CompiledPlan:
        """Compile a task into a signed execution plan.

        Args:
            context: Compilation context with task details
            mode: Execution mode (direct, escalated, retry)

        Returns:
            CompiledPlan with signature
        """
        # Analyze the task
        analysis_context = {
            "active_files": context.active_files,
            "git_status": context.git_status,
            "available_validations": context.available_validations,
        }
        analysis = self.analyzer.analyze(context.user_prompt, analysis_context)

        # Generate plan ID
        plan_id = generate_plan_id()

        # Determine execution mode
        execution_mode = self._determine_mode(mode, analysis)

        # Get allowed tools
        allowed_tools = self._get_allowed_tools(analysis)

        # Get approval requirements
        approval_requirements = self._get_approval_requirements(analysis)

        # Get budgets
        budgets = self._get_budgets(analysis)

        # Determine edit scope
        edit_scope = self._get_edit_scope(context, analysis)

        # Set validity period
        issued_at = datetime.utcnow()
        expires_at = issued_at + timedelta(hours=PLAN_VALIDITY_HOURS)

        # Create plan payload (what gets signed)
        plan_payload = {
            "plan_id": plan_id,
            "session_id": context.session_id,
            "task_id": context.task_id,
            "objective": context.user_prompt,
            "execution_mode": execution_mode,
            "allowed_tools": allowed_tools,
            "workspace_scope": context.workspace_scope,
            "edit_scope": edit_scope,
            "validation_steps": analysis.validation_steps,
            "risk_flags": analysis.risk_flags,
            "budgets": {
                "max_iterations": budgets.max_iterations,
                "max_tool_calls": budgets.max_tool_calls,
                "max_retries": budgets.max_retries,
                "timeout_ms": budgets.timeout_ms,
            },
            "issued_at": issued_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

        # Sign the plan
        sig_info = self.signer.sign(plan_payload)

        # Create compiled plan
        plan = CompiledPlan(
            plan_id=plan_id,
            session_id=context.session_id,
            task_id=context.task_id,
            issued_at=issued_at,
            expires_at=expires_at,
            signature=sig_info.signature,
            signature_alg=sig_info.signature_alg,
            kid=sig_info.kid,
            payload_hash=sig_info.payload_hash,
            objective=context.user_prompt,
            execution_mode=execution_mode,
            allowed_tools=allowed_tools,
            workspace_scope=context.workspace_scope,
            edit_scope=edit_scope,
            validation_steps=analysis.validation_steps,
            approval_requirements=approval_requirements,
            risk_flags=analysis.risk_flags,
            budgets=budgets,
        )

        logger.info(
            f"Plan compiled: plan={plan_id}, task={context.task_id}, "
            f"mode={execution_mode}, tools={len(allowed_tools)}"
        )

        return plan

    def compile_retry(
        self,
        context: CompilationContext,
        previous_plan: CompiledPlan,
        failures: list[dict[str, Any]],
    ) -> CompiledPlan:
        """Compile a retry plan based on previous failures.

        Args:
            context: Compilation context
            previous_plan: The plan that failed
            failures: List of validation failures

        Returns:
            New plan focused on fixing failures
        """
        # Add failure context to prompt
        failure_summary = self._summarize_failures(failures)
        context.user_prompt = (
            f"Fix the following issues from previous attempt:\n{failure_summary}\n\n"
            f"Original task: {context.user_prompt}"
        )

        # Compile with retry mode
        return self.compile(context, mode="retry")

    def _determine_mode(self, requested_mode: str, analysis: TaskAnalysis) -> str:
        """Determine execution mode based on analysis."""
        if requested_mode != "direct":
            return requested_mode

        # Escalate for high-risk tasks
        if analysis.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            return "escalated"

        # Escalate for complex tasks
        if analysis.complexity in (Complexity.COMPLEX, Complexity.CRITICAL):
            return "escalated"

        return "direct"

    def _get_allowed_tools(self, analysis: TaskAnalysis) -> list[str]:
        """Get allowed tools based on analysis."""
        tools = analysis.suggested_tools.copy()

        # Restrict tools for high-risk tasks
        if analysis.risk_level == RiskLevel.CRITICAL:
            # Remove potentially dangerous tools
            restricted = {"bash", "write_file"}
            tools = [t for t in tools if t not in restricted]

        # Always include read-only tools
        read_only = {"read_file", "glob", "grep", "list_directory"}
        for tool in read_only:
            if tool not in tools:
                tools.append(tool)

        return tools

    def _get_approval_requirements(
        self,
        analysis: TaskAnalysis,
    ) -> dict[str, ApprovalRule]:
        """Get approval requirements for tools."""
        requirements = {}

        # Add base approval rules
        for tool, rule in TOOL_APPROVAL_RULES.items():
            if tool in analysis.suggested_tools:
                requirements[tool] = rule

        # Add extra restrictions for high-risk
        if analysis.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            # Require approval for all writes
            if "edit_file" in analysis.suggested_tools:
                requirements["edit_file"] = ApprovalRule(
                    patterns=["*"],
                    require_approval=True,
                )
            if "bash" in analysis.suggested_tools:
                requirements["bash"] = ApprovalRule(
                    patterns=["*"],  # All commands need approval
                    require_approval=True,
                )

        return requirements

    def _get_budgets(self, analysis: TaskAnalysis) -> PlanBudgets:
        """Get execution budgets based on complexity."""
        return DEFAULT_BUDGETS.get(
            analysis.complexity,
            DEFAULT_BUDGETS[Complexity.MODERATE],
        )

    def _get_edit_scope(
        self,
        context: CompilationContext,
        analysis: TaskAnalysis,
    ) -> list[str]:
        """Determine edit scope (file patterns that can be modified)."""
        # Start with active files
        edit_scope = []

        for file_path in context.active_files:
            # Add specific file
            edit_scope.append(file_path)

            # Add sibling files of same type
            if "/" in file_path:
                directory = "/".join(file_path.split("/")[:-1])
                ext = file_path.split(".")[-1] if "." in file_path else "*"
                edit_scope.append(f"{directory}/*.{ext}")

        # Add test files if running tests
        if "test" in analysis.validation_steps:
            edit_scope.extend([
                "tests/**/*.py",
                "test/**/*.py",
                "**/test_*.py",
                "**/*_test.py",
            ])

        # Restrict scope for high-risk
        if analysis.risk_level == RiskLevel.CRITICAL:
            # Only allow explicitly listed files
            edit_scope = context.active_files.copy()

        return list(set(edit_scope))

    def _summarize_failures(self, failures: list[dict[str, Any]]) -> str:
        """Summarize validation failures for retry prompt."""
        lines = []
        for failure in failures[:5]:  # Limit to 5 failures
            test_name = failure.get("test_name", "unknown")
            message = failure.get("message", "")
            file_path = failure.get("file", "")
            line = failure.get("line", 0)

            lines.append(f"- {test_name}")
            if file_path:
                lines.append(f"  File: {file_path}:{line}")
            if message:
                lines.append(f"  Error: {message}")

        return "\n".join(lines)
