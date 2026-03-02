"""Debate Engine - Iterative refinement through perspective challenges.

The debate engine allows perspectives to challenge each other and refine
their positions through multiple rounds of debate, with configurable
turn limits to prevent indefinite execution.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

from .perspectives import (
    Perspective,
    PerspectiveResult,
    PerspectiveType,
)

logger = logging.getLogger(__name__)


@dataclass
class DebateConfig:
    """Configuration for debate engine."""

    # Maximum number of debate rounds
    max_rounds: int = 3

    # Maximum turns per round (prevents runaway debates)
    max_turns: int = 10

    # Minimum confidence delta to continue debate
    min_confidence_delta: float = 0.05

    # Timeout per challenge-response in seconds
    challenge_timeout: float = 30.0

    # Maximum challenges per round
    max_challenges_per_round: int = 6


@dataclass
class DebateRound:
    """Record of a single debate round."""

    round_number: int
    challenges: list[dict[str, Any]]
    responses: list[dict[str, Any]]
    consensus_reached: bool
    approval_count: int
    total_perspectives: int


@dataclass
class DebateResult:
    """Result of a complete debate."""

    final_results: list[PerspectiveResult]
    rounds_completed: int
    total_turns: int
    consensus_reached: bool
    early_exit_reason: Optional[str] = None
    rounds: list[DebateRound] = field(default_factory=list)


class DebateEngine:
    """Engine for iterative refinement through debate.

    Allows perspectives to challenge each other and refine their positions
    through multiple rounds, with turn limits and early exit conditions.
    """

    def __init__(
        self,
        max_rounds: int = 3,
        max_turns: int = 10,
        config: Optional[DebateConfig] = None,
    ):
        """Initialize debate engine.

        Args:
            max_rounds: Maximum debate rounds (deprecated, use config)
            max_turns: Maximum total turns (deprecated, use config)
            config: Debate configuration object
        """
        if config is not None:
            self.config = config
        else:
            self.config = DebateConfig(
                max_rounds=max_rounds,
                max_turns=max_turns,
            )

        self._total_turns = 0

    async def debate(
        self,
        initial_results: list[PerspectiveResult],
        perspectives: list[Perspective],
        llm_callback: Callable[[str, str], Coroutine[Any, Any, str]],
    ) -> list[PerspectiveResult]:
        """Run debate rounds to refine positions.

        Args:
            initial_results: Initial perspective results
            perspectives: Perspective instances
            llm_callback: Async function to call LLM (system, prompt) -> response

        Returns:
            Refined perspective results after debate
        """
        current_results = initial_results
        self._total_turns = 0
        rounds_completed = 0

        for round_num in range(self.config.max_rounds):
            # Check turn limit
            if self._total_turns >= self.config.max_turns:
                logger.info(f"Debate ended: turn limit ({self.config.max_turns}) reached")
                break

            # Check if consensus reached
            approvals = sum(1 for r in current_results if r.approved)
            if approvals == len(current_results) or approvals == 0:
                logger.info(f"Debate ended: full consensus at round {round_num}")
                break

            # Generate challenges
            challenges = self._generate_challenges(current_results)
            if not challenges:
                logger.info(f"Debate ended: no challenges to make at round {round_num}")
                break

            # Get responses to challenges
            new_results = []
            for result, perspective in zip(current_results, perspectives):
                # Check turn limit before each response
                if self._total_turns >= self.config.max_turns:
                    new_results.append(result)
                    continue

                relevant_challenges = [
                    c for c in challenges
                    if c["target"] == result.perspective_type
                ]

                if relevant_challenges:
                    self._total_turns += 1
                    try:
                        refined = await asyncio.wait_for(
                            self._get_refined_position(
                                result, relevant_challenges, perspective, llm_callback
                            ),
                            timeout=self.config.challenge_timeout,
                        )
                        new_results.append(refined)
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"Debate response timeout for {perspective.perspective_type}"
                        )
                        new_results.append(result)
                    except Exception as e:
                        logger.error(f"Debate error for {perspective.perspective_type}: {e}")
                        new_results.append(result)
                else:
                    new_results.append(result)

            current_results = new_results
            rounds_completed = round_num + 1

            # Check for convergence (no position changes)
            if self._check_convergence(initial_results, current_results):
                logger.info(f"Debate converged at round {round_num}")
                break

        logger.info(
            f"Debate completed: {rounds_completed} rounds, {self._total_turns} turns"
        )
        return current_results

    async def debate_with_details(
        self,
        initial_results: list[PerspectiveResult],
        perspectives: list[Perspective],
        llm_callback: Callable[[str, str], Coroutine[Any, Any, str]],
    ) -> DebateResult:
        """Run debate and return detailed results.

        Same as debate() but returns full DebateResult with round details.
        """
        current_results = initial_results
        self._total_turns = 0
        rounds: list[DebateRound] = []
        early_exit_reason = None

        for round_num in range(self.config.max_rounds):
            # Check turn limit
            if self._total_turns >= self.config.max_turns:
                early_exit_reason = f"Turn limit ({self.config.max_turns}) reached"
                break

            # Check if consensus reached
            approvals = sum(1 for r in current_results if r.approved)
            if approvals == len(current_results):
                early_exit_reason = "Full approval consensus"
                break
            if approvals == 0:
                early_exit_reason = "Full rejection consensus"
                break

            # Generate challenges
            challenges = self._generate_challenges(current_results)
            if not challenges:
                early_exit_reason = "No challenges to make"
                break

            # Process round
            round_responses: list[dict[str, Any]] = []
            new_results = []

            for result, perspective in zip(current_results, perspectives):
                if self._total_turns >= self.config.max_turns:
                    new_results.append(result)
                    continue

                relevant_challenges = [
                    c for c in challenges
                    if c["target"] == result.perspective_type
                ]

                if relevant_challenges:
                    self._total_turns += 1
                    try:
                        refined = await asyncio.wait_for(
                            self._get_refined_position(
                                result, relevant_challenges, perspective, llm_callback
                            ),
                            timeout=self.config.challenge_timeout,
                        )
                        round_responses.append({
                            "perspective": result.perspective_type.value,
                            "changed": refined.approved != result.approved,
                            "new_position": "approved" if refined.approved else "rejected",
                        })
                        new_results.append(refined)
                    except Exception as e:
                        round_responses.append({
                            "perspective": result.perspective_type.value,
                            "error": str(e),
                        })
                        new_results.append(result)
                else:
                    new_results.append(result)

            # Record round
            round_approvals = sum(1 for r in new_results if r.approved)
            rounds.append(DebateRound(
                round_number=round_num + 1,
                challenges=challenges,
                responses=round_responses,
                consensus_reached=round_approvals in (0, len(new_results)),
                approval_count=round_approvals,
                total_perspectives=len(new_results),
            ))

            current_results = new_results

            # Check convergence
            if self._check_convergence(initial_results, current_results):
                early_exit_reason = "Positions converged"
                break

        final_approvals = sum(1 for r in current_results if r.approved)
        return DebateResult(
            final_results=current_results,
            rounds_completed=len(rounds),
            total_turns=self._total_turns,
            consensus_reached=final_approvals in (0, len(current_results)),
            early_exit_reason=early_exit_reason,
            rounds=rounds,
        )

    def _generate_challenges(
        self,
        results: list[PerspectiveResult],
    ) -> list[dict[str, Any]]:
        """Generate challenges between disagreeing perspectives."""
        challenges = []

        approving = [r for r in results if r.approved]
        rejecting = [r for r in results if not r.approved]

        # Rejecting perspectives challenge approving ones
        for rejector in rejecting:
            for approver in approving:
                if rejector.issues:
                    challenges.append({
                        "challenger": rejector.perspective_type,
                        "target": approver.perspective_type,
                        "challenge": f"How do you address: {rejector.issues[0].message}",
                    })

        # Approving perspectives challenge rejecting ones
        for approver in approving:
            for rejector in rejecting:
                challenges.append({
                    "challenger": approver.perspective_type,
                    "target": rejector.perspective_type,
                    "challenge": "Is this issue critical enough to block? Consider the task requirements.",
                })

        return challenges[:self.config.max_challenges_per_round]

    async def _get_refined_position(
        self,
        original: PerspectiveResult,
        challenges: list[dict[str, Any]],
        perspective: Perspective,
        llm_callback: Callable[[str, str], Coroutine[Any, Any, str]],
    ) -> PerspectiveResult:
        """Get refined position after considering challenges."""
        challenge_text = "\n".join(
            f"- {c['challenger'].value}: {c['challenge']}"
            for c in challenges
        )

        prompt = f"""Your original position: {'APPROVED' if original.approved else 'REJECTED'}
Your reasoning: {original.reasoning[:500]}

Challenges from other council members:
{challenge_text}

Reconsider your position. You may:
1. Maintain your position with additional justification
2. Change your position if the challenges are valid

Respond with **[APPROVED]** or **[REJECTED]** and updated reasoning."""

        try:
            response = await llm_callback(
                perspective.get_system_prompt(),
                prompt,
            )
            return perspective.parse_response(response)
        except Exception as e:
            logger.error(f"Failed to get refined position: {e}")
            return original

    def _check_convergence(
        self,
        initial: list[PerspectiveResult],
        current: list[PerspectiveResult],
    ) -> bool:
        """Check if positions have converged (no changes from initial)."""
        if len(initial) != len(current):
            return False

        for init, curr in zip(initial, current):
            if init.approved != curr.approved:
                return False

        return True


# Factory functions


def create_standard_debate_engine() -> DebateEngine:
    """Create a standard debate engine."""
    return DebateEngine(config=DebateConfig())


def create_quick_debate_engine() -> DebateEngine:
    """Create a quick debate engine with limited rounds."""
    return DebateEngine(config=DebateConfig(
        max_rounds=2,
        max_turns=6,
        max_challenges_per_round=4,
    ))


def create_thorough_debate_engine() -> DebateEngine:
    """Create a thorough debate engine with more rounds."""
    return DebateEngine(config=DebateConfig(
        max_rounds=5,
        max_turns=20,
        max_challenges_per_round=8,
        challenge_timeout=60.0,
    ))
