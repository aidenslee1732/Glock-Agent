"""Skill composition system - allows skills to invoke and chain other skills.

This module provides:
- CompositeSkill: A skill that chains multiple skills together
- SkillInvoker: Context extension allowing skills to invoke other skills
- Result chaining: Pass output from one skill as input to the next
- Dependency resolution: Ensure skills run in correct order
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

from .base import Skill, SkillResult
from .registry import SkillRegistry

logger = logging.getLogger(__name__)


@dataclass
class SkillStep:
    """A single step in a composite skill.

    Attributes:
        skill_name: Name of the skill to invoke
        args: Static arguments or callable that receives previous result
        condition: Optional condition to check before running
        on_failure: What to do on failure ("stop", "skip", "continue")
        result_key: Key to store result under in chain context
    """
    skill_name: str
    args: Union[str, Callable[[dict], str]] = ""
    condition: Optional[Callable[[dict], bool]] = None
    on_failure: str = "stop"  # "stop", "skip", "continue"
    result_key: Optional[str] = None

    def get_args(self, chain_context: dict[str, Any]) -> str:
        """Get arguments, resolving callables if needed.

        Args:
            chain_context: Context from previous skill results

        Returns:
            Arguments string
        """
        if callable(self.args):
            return self.args(chain_context)
        return self.args

    def should_run(self, chain_context: dict[str, Any]) -> bool:
        """Check if this step should run.

        Args:
            chain_context: Context from previous skill results

        Returns:
            True if step should run
        """
        if self.condition is None:
            return True
        try:
            return self.condition(chain_context)
        except Exception as e:
            logger.warning(f"Condition check failed for {self.skill_name}: {e}")
            return False


@dataclass
class CompositeSkillResult(SkillResult):
    """Result from a composite skill execution.

    Extends SkillResult with chain-specific data.
    """
    step_results: list[dict[str, Any]] = field(default_factory=list)
    chain_context: dict[str, Any] = field(default_factory=dict)


class SkillInvoker:
    """Provides skill invocation capabilities to skill handlers.

    This is passed in the context to skill handlers, allowing them
    to invoke other skills.
    """

    def __init__(self, registry: SkillRegistry, base_context: dict[str, Any]):
        """Initialize the invoker.

        Args:
            registry: Skill registry to look up skills
            base_context: Base context to pass to invoked skills
        """
        self._registry = registry
        self._base_context = base_context
        self._call_stack: list[str] = []  # Prevent infinite recursion

    async def invoke(
        self,
        skill_name: str,
        args: str = "",
        extra_context: Optional[dict] = None,
    ) -> SkillResult:
        """Invoke a skill by name.

        Args:
            skill_name: Name of the skill to invoke
            args: Arguments to pass to the skill
            extra_context: Additional context to merge

        Returns:
            SkillResult from the invoked skill
        """
        # Check for recursion
        if skill_name in self._call_stack:
            return SkillResult(
                status="failed",
                error=f"Recursive skill invocation detected: {' -> '.join(self._call_stack)} -> {skill_name}",
            )

        skill = self._registry.get(skill_name)
        if not skill:
            return SkillResult(
                status="failed",
                error=f"Unknown skill: {skill_name}",
            )

        # Build context
        context = {**self._base_context}
        if extra_context:
            context.update(extra_context)

        # Add invoker to context (with updated call stack)
        child_invoker = SkillInvoker(
            self._registry,
            self._base_context,
        )
        child_invoker._call_stack = self._call_stack + [skill_name]
        context["skill_invoker"] = child_invoker

        # Invoke the skill
        self._call_stack.append(skill_name)
        try:
            logger.debug(f"Invoking skill: {skill_name} (depth: {len(self._call_stack)})")
            result = await skill.invoke(args, context)
            logger.debug(f"Skill {skill_name} completed: {result.status}")
            return result
        finally:
            self._call_stack.pop()

    async def invoke_parallel(
        self,
        skill_calls: list[tuple[str, str]],
        extra_context: Optional[dict] = None,
    ) -> list[SkillResult]:
        """Invoke multiple skills in parallel.

        Args:
            skill_calls: List of (skill_name, args) tuples
            extra_context: Additional context to merge

        Returns:
            List of SkillResults in same order as input
        """
        tasks = [
            self.invoke(name, args, extra_context)
            for name, args in skill_calls
        ]
        return await asyncio.gather(*tasks)


class CompositeSkill(Skill):
    """A skill that chains multiple skills together.

    Supports:
    - Sequential execution with result passing
    - Conditional steps
    - Failure handling strategies
    - Result aggregation
    """

    def __init__(
        self,
        name: str,
        description: str,
        steps: list[SkillStep],
        registry: SkillRegistry,
        aliases: Optional[list[str]] = None,
        category: str = "composite",
        aggregate_output: bool = True,
    ):
        """Initialize a composite skill.

        Args:
            name: Skill name
            description: Description
            steps: List of skill steps to execute
            registry: Skill registry for lookups
            aliases: Alternative names
            category: Category
            aggregate_output: Whether to combine step outputs
        """
        self._steps = steps
        self._registry = registry
        self._aggregate_output = aggregate_output

        super().__init__(
            name=name,
            description=description,
            handler=self._execute,
            aliases=aliases or [],
            category=category,
        )

    async def _execute(
        self,
        args: str,
        context: dict[str, Any],
    ) -> CompositeSkillResult:
        """Execute the composite skill.

        Args:
            args: Initial arguments
            context: Execution context

        Returns:
            CompositeSkillResult with all step results
        """
        chain_context: dict[str, Any] = {
            "initial_args": args,
            "results": {},
            "outputs": [],
        }

        step_results: list[dict[str, Any]] = []

        # Create invoker
        invoker = SkillInvoker(self._registry, context)

        for i, step in enumerate(self._steps):
            step_info = {
                "index": i,
                "skill": step.skill_name,
                "status": "pending",
            }

            # Check condition
            if not step.should_run(chain_context):
                step_info["status"] = "skipped"
                step_info["reason"] = "condition not met"
                step_results.append(step_info)
                continue

            # Get arguments
            try:
                step_args = step.get_args(chain_context)
            except Exception as e:
                step_info["status"] = "failed"
                step_info["error"] = f"Failed to get arguments: {e}"
                step_results.append(step_info)

                if step.on_failure == "stop":
                    return CompositeSkillResult(
                        status="failed",
                        error=f"Step {i} ({step.skill_name}) failed: {e}",
                        step_results=step_results,
                        chain_context=chain_context,
                    )
                continue

            # Execute step
            result = await invoker.invoke(step.skill_name, step_args)

            step_info["status"] = result.status
            step_info["output"] = result.output
            if result.error:
                step_info["error"] = result.error
            step_results.append(step_info)

            # Store result in chain context
            result_key = step.result_key or step.skill_name
            chain_context["results"][result_key] = result
            chain_context["outputs"].append(result.output)
            chain_context["last_result"] = result

            # Handle failure
            if result.status == "failed":
                if step.on_failure == "stop":
                    return CompositeSkillResult(
                        status="failed",
                        error=f"Step {i} ({step.skill_name}) failed: {result.error}",
                        step_results=step_results,
                        chain_context=chain_context,
                    )
                elif step.on_failure == "skip":
                    continue
                # "continue" just proceeds

        # Aggregate output
        if self._aggregate_output:
            output_parts = []
            for step_result in step_results:
                if step_result["status"] == "completed":
                    output_parts.append(
                        f"### {step_result['skill']}\n{step_result.get('output', '')}"
                    )
            output = "\n\n".join(output_parts)
        else:
            # Just use last result's output
            last_result = chain_context.get("last_result")
            output = last_result.output if last_result else ""

        return CompositeSkillResult(
            status="completed",
            output=output,
            step_results=step_results,
            chain_context=chain_context,
            metadata={
                "steps_executed": len([s for s in step_results if s["status"] != "skipped"]),
                "steps_skipped": len([s for s in step_results if s["status"] == "skipped"]),
                "steps_failed": len([s for s in step_results if s["status"] == "failed"]),
            },
        )


class CompositeSkillBuilder:
    """Builder for creating composite skills with a fluent API."""

    def __init__(self, name: str, registry: SkillRegistry):
        """Initialize the builder.

        Args:
            name: Skill name
            registry: Skill registry
        """
        self._name = name
        self._registry = registry
        self._description = ""
        self._steps: list[SkillStep] = []
        self._aliases: list[str] = []
        self._category = "composite"
        self._aggregate_output = True

    def description(self, desc: str) -> "CompositeSkillBuilder":
        """Set the description.

        Args:
            desc: Description

        Returns:
            Self for chaining
        """
        self._description = desc
        return self

    def aliases(self, *names: str) -> "CompositeSkillBuilder":
        """Add aliases.

        Args:
            names: Alias names

        Returns:
            Self for chaining
        """
        self._aliases.extend(names)
        return self

    def category(self, cat: str) -> "CompositeSkillBuilder":
        """Set the category.

        Args:
            cat: Category name

        Returns:
            Self for chaining
        """
        self._category = cat
        return self

    def step(
        self,
        skill_name: str,
        args: Union[str, Callable[[dict], str]] = "",
        condition: Optional[Callable[[dict], bool]] = None,
        on_failure: str = "stop",
        result_key: Optional[str] = None,
    ) -> "CompositeSkillBuilder":
        """Add a step.

        Args:
            skill_name: Name of skill to invoke
            args: Arguments or callable
            condition: Condition to check
            on_failure: Failure handling
            result_key: Key for result storage

        Returns:
            Self for chaining
        """
        self._steps.append(SkillStep(
            skill_name=skill_name,
            args=args,
            condition=condition,
            on_failure=on_failure,
            result_key=result_key,
        ))
        return self

    def aggregate_output(self, aggregate: bool) -> "CompositeSkillBuilder":
        """Set whether to aggregate output.

        Args:
            aggregate: True to aggregate

        Returns:
            Self for chaining
        """
        self._aggregate_output = aggregate
        return self

    def build(self) -> CompositeSkill:
        """Build the composite skill.

        Returns:
            CompositeSkill instance
        """
        return CompositeSkill(
            name=self._name,
            description=self._description,
            steps=self._steps,
            registry=self._registry,
            aliases=self._aliases,
            category=self._category,
            aggregate_output=self._aggregate_output,
        )


# Built-in composite skills
def create_full_review_skill(registry: SkillRegistry) -> CompositeSkill:
    """Create a comprehensive review skill.

    Chains: security-scan -> review -> (optional) format

    Args:
        registry: Skill registry

    Returns:
        CompositeSkill for full review
    """
    return (
        CompositeSkillBuilder("full-review", registry)
        .description("Comprehensive code review with security scan")
        .aliases("fr", "comprehensive-review")
        .category("development")
        .step("security-scan", on_failure="continue")
        .step("review")
        .build()
    )


def create_ship_skill(registry: SkillRegistry) -> CompositeSkill:
    """Create a skill for shipping code (review + commit + push).

    Chains: review -> commit -> (push suggested, not auto)

    Args:
        registry: Skill registry

    Returns:
        CompositeSkill for shipping
    """
    return (
        CompositeSkillBuilder("ship", registry)
        .description("Review changes and commit (manual push)")
        .aliases("s")
        .category("git")
        .step(
            "review",
            condition=lambda ctx: bool(ctx.get("initial_args", "").strip() or True),
        )
        .step(
            "commit",
            args=lambda ctx: ctx.get("initial_args", ""),
            on_failure="stop",
        )
        .aggregate_output(False)  # Just show commit result
        .build()
    )


def register_composite_skills(registry: SkillRegistry) -> int:
    """Register built-in composite skills.

    Args:
        registry: Skill registry

    Returns:
        Number of skills registered
    """
    composites = [
        create_full_review_skill(registry),
        create_ship_skill(registry),
    ]

    for skill in composites:
        registry.register(skill)

    logger.info(f"Registered {len(composites)} composite skills")
    return len(composites)
