"""Synthesis Engine - Aggregates council perspectives into final decision.

The synthesis engine collects votes from all perspectives, detects conflicts,
builds consensus, and produces a final recommendation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .perspectives import (
    Perspective,
    PerspectiveResult,
    PerspectiveType,
    Issue,
    Severity,
)

logger = logging.getLogger(__name__)


class VoteType(str, Enum):
    """Type of vote from a perspective."""
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"


class ConflictType(str, Enum):
    """Type of conflict between perspectives."""
    DIRECT_OPPOSITION = "direct_opposition"  # One approves, one rejects
    SEVERITY_DISAGREEMENT = "severity_disagreement"  # Different severity for same issue
    RECOMMENDATION_CONFLICT = "recommendation_conflict"  # Conflicting suggestions


@dataclass
class Vote:
    """A vote from a single perspective."""
    perspective_type: PerspectiveType
    vote: VoteType
    weight: float
    confidence: float
    reasoning: str
    issues_count: int
    critical_issues: int

    @property
    def weighted_score(self) -> float:
        """Calculate weighted vote score."""
        base = 1.0 if self.vote == VoteType.APPROVE else -1.0 if self.vote == VoteType.REJECT else 0.0
        return base * self.weight * self.confidence


@dataclass
class Conflict:
    """A conflict detected between perspectives."""
    conflict_type: ConflictType
    perspectives: list[PerspectiveType]
    description: str
    resolution: Optional[str] = None
    resolved: bool = False


@dataclass
class ConsensusResult:
    """Final consensus from synthesis."""
    approved: bool
    confidence: float
    vote_summary: dict[VoteType, int]
    weighted_score: float
    critical_issues: list[Issue]
    all_issues: list[Issue]
    conflicts: list[Conflict]
    recommendations: list[str]
    reasoning: str
    dissenting_perspectives: list[PerspectiveType]
    metadata: dict[str, Any] = field(default_factory=dict)


class SynthesisEngine:
    """Synthesizes multiple perspective results into final decision.

    The engine uses weighted voting with conflict detection and
    resolution to produce a final recommendation.
    """

    def __init__(
        self,
        approval_threshold: float = 0.6,
        critical_veto: bool = True,
        require_security_approval: bool = True,
        require_correctness_approval: bool = True,
    ):
        """Initialize synthesis engine.

        Args:
            approval_threshold: Weighted vote threshold for approval (0-1)
            critical_veto: If True, any CRITICAL issue causes rejection
            require_security_approval: Security perspective must approve
            require_correctness_approval: Correctness perspective must approve
        """
        self.approval_threshold = approval_threshold
        self.critical_veto = critical_veto
        self.require_security_approval = require_security_approval
        self.require_correctness_approval = require_correctness_approval

    def synthesize(
        self,
        results: list[PerspectiveResult],
        perspectives: list[Perspective],
    ) -> ConsensusResult:
        """Synthesize perspective results into consensus.

        Args:
            results: Results from each perspective
            perspectives: The perspective instances used

        Returns:
            ConsensusResult with final decision
        """
        # Build perspective lookup
        perspective_lookup = {p.perspective_type: p for p in perspectives}

        # Collect votes
        votes = self._collect_votes(results, perspective_lookup)

        # Detect conflicts
        conflicts = self._detect_conflicts(results)

        # Try to resolve conflicts
        self._resolve_conflicts(conflicts, results)

        # Aggregate issues
        all_issues = self._aggregate_issues(results)
        critical_issues = [i for i in all_issues if i.severity == Severity.CRITICAL]

        # Calculate weighted score
        weighted_score = sum(v.weighted_score for v in votes)
        max_possible = sum(v.weight * v.confidence for v in votes if v.vote != VoteType.ABSTAIN)
        normalized_score = (weighted_score / max_possible + 1) / 2 if max_possible > 0 else 0.5

        # Count votes
        vote_summary = {
            VoteType.APPROVE: sum(1 for v in votes if v.vote == VoteType.APPROVE),
            VoteType.REJECT: sum(1 for v in votes if v.vote == VoteType.REJECT),
            VoteType.ABSTAIN: sum(1 for v in votes if v.vote == VoteType.ABSTAIN),
        }

        # Determine approval
        approved, reasoning = self._determine_approval(
            votes=votes,
            normalized_score=normalized_score,
            critical_issues=critical_issues,
            results=results,
        )

        # Find dissenting perspectives
        dissenting = self._find_dissenting(votes, approved)

        # Aggregate recommendations
        recommendations = self._aggregate_recommendations(results)

        # Calculate confidence
        confidence = self._calculate_confidence(votes, conflicts)

        return ConsensusResult(
            approved=approved,
            confidence=confidence,
            vote_summary=vote_summary,
            weighted_score=normalized_score,
            critical_issues=critical_issues,
            all_issues=all_issues,
            conflicts=conflicts,
            recommendations=recommendations,
            reasoning=reasoning,
            dissenting_perspectives=dissenting,
            metadata={
                "total_votes": len(votes),
                "max_possible_score": max_possible,
                "raw_weighted_score": weighted_score,
            }
        )

    def _collect_votes(
        self,
        results: list[PerspectiveResult],
        perspectives: dict[PerspectiveType, Perspective],
    ) -> list[Vote]:
        """Collect votes from all perspectives."""
        votes = []

        for result in results:
            perspective = perspectives.get(result.perspective_type)
            weight = perspective.weight if perspective else 1.0

            vote_type = VoteType.APPROVE if result.approved else VoteType.REJECT

            votes.append(Vote(
                perspective_type=result.perspective_type,
                vote=vote_type,
                weight=weight,
                confidence=result.confidence,
                reasoning=result.reasoning[:200],  # Truncate for storage
                issues_count=len(result.issues),
                critical_issues=len(result.critical_issues),
            ))

        return votes

    def _detect_conflicts(self, results: list[PerspectiveResult]) -> list[Conflict]:
        """Detect conflicts between perspective results."""
        conflicts = []

        # Check for direct opposition (one approves, one rejects on same topic)
        approving = [r for r in results if r.approved]
        rejecting = [r for r in results if not r.approved]

        if approving and rejecting:
            # Check if they're discussing same issues
            approving_categories = {i.category for r in approving for i in r.issues}
            rejecting_categories = {i.category for r in rejecting for i in r.issues}
            overlapping = approving_categories & rejecting_categories

            if overlapping:
                conflicts.append(Conflict(
                    conflict_type=ConflictType.DIRECT_OPPOSITION,
                    perspectives=[r.perspective_type for r in approving + rejecting],
                    description=f"Perspectives disagree on: {', '.join(overlapping)}",
                ))

        # Check for severity disagreements
        issue_severities: dict[str, list[tuple[PerspectiveType, Severity]]] = {}
        for result in results:
            for issue in result.issues:
                key = issue.message[:50]  # Normalize by truncating
                if key not in issue_severities:
                    issue_severities[key] = []
                issue_severities[key].append((result.perspective_type, issue.severity))

        for issue_key, severities in issue_severities.items():
            unique_severities = set(s for _, s in severities)
            if len(unique_severities) > 1:
                conflicts.append(Conflict(
                    conflict_type=ConflictType.SEVERITY_DISAGREEMENT,
                    perspectives=[p for p, _ in severities],
                    description=f"Severity disagreement on '{issue_key[:30]}...': {unique_severities}",
                ))

        return conflicts

    def _resolve_conflicts(
        self,
        conflicts: list[Conflict],
        results: list[PerspectiveResult],
    ) -> None:
        """Attempt to resolve detected conflicts."""
        for conflict in conflicts:
            if conflict.conflict_type == ConflictType.DIRECT_OPPOSITION:
                # Resolution: Higher confidence wins, or security/correctness takes priority
                relevant_results = [r for r in results if r.perspective_type in conflict.perspectives]

                # Security and correctness have veto power
                priority_types = {PerspectiveType.SECURITY, PerspectiveType.CORRECTNESS}
                priority_results = [r for r in relevant_results if r.perspective_type in priority_types]

                if priority_results:
                    # Priority perspective decision wins
                    if any(not r.approved for r in priority_results):
                        conflict.resolution = "Priority perspective (security/correctness) rejected"
                    else:
                        conflict.resolution = "Priority perspective (security/correctness) approved"
                    conflict.resolved = True
                else:
                    # Higher confidence wins
                    highest = max(relevant_results, key=lambda r: r.confidence)
                    conflict.resolution = f"{highest.perspective_type.value} wins by confidence ({highest.confidence:.2f})"
                    conflict.resolved = True

            elif conflict.conflict_type == ConflictType.SEVERITY_DISAGREEMENT:
                # Resolution: Take highest severity
                conflict.resolution = "Using highest reported severity"
                conflict.resolved = True

    def _determine_approval(
        self,
        votes: list[Vote],
        normalized_score: float,
        critical_issues: list[Issue],
        results: list[PerspectiveResult],
    ) -> tuple[bool, str]:
        """Determine final approval decision."""
        reasons = []

        # Check critical veto
        if self.critical_veto and critical_issues:
            return False, f"REJECTED: {len(critical_issues)} critical issues found"

        # Check required approvals
        if self.require_security_approval:
            security_results = [r for r in results if r.perspective_type == PerspectiveType.SECURITY]
            if security_results and not security_results[0].approved:
                reasons.append("Security perspective rejected")

        if self.require_correctness_approval:
            correctness_results = [r for r in results if r.perspective_type == PerspectiveType.CORRECTNESS]
            if correctness_results and not correctness_results[0].approved:
                reasons.append("Correctness perspective rejected")

        if reasons:
            return False, f"REJECTED: {'; '.join(reasons)}"

        # Check threshold
        if normalized_score >= self.approval_threshold:
            return True, f"APPROVED: Weighted score {normalized_score:.2f} >= threshold {self.approval_threshold}"
        else:
            return False, f"REJECTED: Weighted score {normalized_score:.2f} < threshold {self.approval_threshold}"

    def _find_dissenting(self, votes: list[Vote], approved: bool) -> list[PerspectiveType]:
        """Find perspectives that dissented from final decision."""
        if approved:
            return [v.perspective_type for v in votes if v.vote == VoteType.REJECT]
        else:
            return [v.perspective_type for v in votes if v.vote == VoteType.APPROVE]

    def _aggregate_issues(self, results: list[PerspectiveResult]) -> list[Issue]:
        """Aggregate and deduplicate issues from all perspectives."""
        all_issues = []
        seen_messages = set()

        # Sort by severity (critical first)
        severity_order = {
            Severity.CRITICAL: 0,
            Severity.ERROR: 1,
            Severity.WARNING: 2,
            Severity.INFO: 3,
        }

        for result in results:
            for issue in sorted(result.issues, key=lambda i: severity_order.get(i.severity, 4)):
                # Simple deduplication by message
                msg_key = issue.message[:50].lower()
                if msg_key not in seen_messages:
                    seen_messages.add(msg_key)
                    all_issues.append(issue)

        return sorted(all_issues, key=lambda i: severity_order.get(i.severity, 4))

    def _aggregate_recommendations(self, results: list[PerspectiveResult]) -> list[str]:
        """Aggregate recommendations from all perspectives."""
        recommendations = []
        seen = set()

        for result in results:
            for suggestion in result.suggestions:
                if suggestion.lower() not in seen:
                    seen.add(suggestion.lower())
                    recommendations.append(f"[{result.perspective_type.value}] {suggestion}")

        return recommendations

    def _calculate_confidence(self, votes: list[Vote], conflicts: list[Conflict]) -> float:
        """Calculate overall confidence in the decision."""
        if not votes:
            return 0.0

        # Base confidence from average vote confidence
        avg_confidence = sum(v.confidence for v in votes) / len(votes)

        # Reduce confidence for unresolved conflicts
        unresolved = sum(1 for c in conflicts if not c.resolved)
        conflict_penalty = unresolved * 0.1

        # Reduce confidence for mixed votes
        vote_types = set(v.vote for v in votes if v.vote != VoteType.ABSTAIN)
        mixed_penalty = 0.15 if len(vote_types) > 1 else 0

        return max(0.0, min(1.0, avg_confidence - conflict_penalty - mixed_penalty))
