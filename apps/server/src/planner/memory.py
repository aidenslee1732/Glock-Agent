"""
Memory management for Glock server.

Handles:
- User preferences (learned and explicit)
- Cross-session memory
- Task history for routing decisions
- Pattern learning
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Set
from collections import defaultdict

from ..storage.redis import RedisClient
from ..storage.repositories.users import UserRepository


logger = logging.getLogger(__name__)


@dataclass
class UserPreferences:
    """User preferences for task execution."""
    # Code style preferences
    code_style: Dict[str, Any] = field(default_factory=dict)

    # Tool preferences
    preferred_tools: Dict[str, str] = field(default_factory=dict)

    # Validation preferences
    validation_level: str = "standard"  # strict, standard, minimal
    auto_test: bool = True
    auto_lint: bool = True
    auto_typecheck: bool = True

    # Approval preferences
    auto_approve_edits: bool = False
    auto_approve_bash: bool = False

    # Learning settings
    learning_enabled: bool = True

    # Confidence scores for learned preferences
    confidence: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'code_style': self.code_style,
            'preferred_tools': self.preferred_tools,
            'validation_level': self.validation_level,
            'auto_test': self.auto_test,
            'auto_lint': self.auto_lint,
            'auto_typecheck': self.auto_typecheck,
            'auto_approve_edits': self.auto_approve_edits,
            'auto_approve_bash': self.auto_approve_bash,
            'learning_enabled': self.learning_enabled,
            'confidence': self.confidence
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserPreferences":
        return cls(
            code_style=data.get('code_style', {}),
            preferred_tools=data.get('preferred_tools', {}),
            validation_level=data.get('validation_level', 'standard'),
            auto_test=data.get('auto_test', True),
            auto_lint=data.get('auto_lint', True),
            auto_typecheck=data.get('auto_typecheck', True),
            auto_approve_edits=data.get('auto_approve_edits', False),
            auto_approve_bash=data.get('auto_approve_bash', False),
            learning_enabled=data.get('learning_enabled', True),
            confidence=data.get('confidence', {})
        )


@dataclass
class TaskHistory:
    """Historical record of a task execution."""
    task_id: str
    task_type: str
    risk_level: str
    success: bool
    duration_ms: int
    retry_count: int
    tools_used: List[str]
    files_modified: List[str]
    validation_results: Dict[str, bool]
    timestamp: datetime


@dataclass
class MemoryConfig:
    """Configuration for memory management."""
    # Caching
    preference_cache_ttl: int = 3600  # 1 hour
    history_cache_ttl: int = 86400  # 24 hours

    # Learning
    confidence_decay: float = 0.95
    min_observations: int = 3
    max_observations: int = 100

    # History
    max_history_per_user: int = 1000
    history_retention_days: int = 90


class MemoryManager:
    """
    Manages user preferences and cross-session memory.

    Features:
    - User preference storage and retrieval
    - Preference learning from observations
    - Task history for routing decisions
    - Cross-session context
    """

    def __init__(
        self,
        redis: RedisClient,
        user_repo: UserRepository,
        config: Optional[MemoryConfig] = None
    ):
        self.redis = redis
        self.user_repo = user_repo
        self.config = config or MemoryConfig()

        # In-memory caches
        self._preference_cache: Dict[str, UserPreferences] = {}
        self._history_cache: Dict[str, List[TaskHistory]] = {}

    # Preference management

    async def get_user_preferences(self, user_id: str) -> UserPreferences:
        """Get user preferences, from cache or database."""
        # Check cache
        if user_id in self._preference_cache:
            return self._preference_cache[user_id]

        # Check Redis
        cached = await self.redis.get(f"prefs:{user_id}")
        if cached:
            prefs = UserPreferences.from_dict(json.loads(cached))
            self._preference_cache[user_id] = prefs
            return prefs

        # Load from database
        prefs = await self._load_preferences_from_db(user_id)

        # Cache
        self._preference_cache[user_id] = prefs
        await self.redis.setex(
            f"prefs:{user_id}",
            self.config.preference_cache_ttl,
            json.dumps(prefs.to_dict())
        )

        return prefs

    async def _load_preferences_from_db(self, user_id: str) -> UserPreferences:
        """Load preferences from database."""
        # This would query the user_preferences table
        # For now, return defaults
        return UserPreferences()

    async def update_user_preferences(
        self,
        user_id: str,
        updates: Dict[str, Any]
    ) -> UserPreferences:
        """Update user preferences explicitly."""
        prefs = await self.get_user_preferences(user_id)

        # Apply updates
        for key, value in updates.items():
            if hasattr(prefs, key):
                setattr(prefs, key, value)
                # Set high confidence for explicit updates
                prefs.confidence[key] = 1.0

        # Save to database
        await self._save_preferences_to_db(user_id, prefs)

        # Update caches
        self._preference_cache[user_id] = prefs
        await self.redis.setex(
            f"prefs:{user_id}",
            self.config.preference_cache_ttl,
            json.dumps(prefs.to_dict())
        )

        return prefs

    async def _save_preferences_to_db(
        self,
        user_id: str,
        prefs: UserPreferences
    ) -> None:
        """Save preferences to database."""
        # This would update the user_preferences table
        pass

    # Preference learning

    async def record_observation(
        self,
        user_id: str,
        observation_type: str,
        value: Any,
        signal_strength: float = 1.0,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Record an observation for preference learning."""
        prefs = await self.get_user_preferences(user_id)

        if not prefs.learning_enabled:
            return

        # Store observation
        observation = {
            'type': observation_type,
            'value': value,
            'strength': signal_strength,
            'context': context or {},
            'timestamp': datetime.utcnow().isoformat()
        }

        await self.redis.lpush(
            f"observations:{user_id}",
            json.dumps(observation)
        )

        # Trim to max observations
        await self.redis.ltrim(
            f"observations:{user_id}",
            0,
            self.config.max_observations - 1
        )

        # Trigger learning if enough observations
        obs_count = await self.redis.llen(f"observations:{user_id}")
        if obs_count >= self.config.min_observations:
            await self._learn_preferences(user_id)

    async def _learn_preferences(self, user_id: str) -> None:
        """Learn preferences from observations."""
        # Get recent observations
        raw_observations = await self.redis.lrange(
            f"observations:{user_id}",
            0,
            -1
        )

        observations = [json.loads(o) for o in raw_observations]

        if not observations:
            return

        prefs = await self.get_user_preferences(user_id)

        # Group observations by type
        by_type: Dict[str, List[Dict]] = defaultdict(list)
        for obs in observations:
            by_type[obs['type']].append(obs)

        # Learn from each type
        for obs_type, type_obs in by_type.items():
            await self._learn_from_observations(prefs, obs_type, type_obs)

        # Save updated preferences
        await self._save_preferences_to_db(user_id, prefs)

        # Update caches
        self._preference_cache[user_id] = prefs
        await self.redis.setex(
            f"prefs:{user_id}",
            self.config.preference_cache_ttl,
            json.dumps(prefs.to_dict())
        )

    async def _learn_from_observations(
        self,
        prefs: UserPreferences,
        obs_type: str,
        observations: List[Dict]
    ) -> None:
        """Learn preferences from a set of observations."""
        if not observations:
            return

        # Calculate weighted average/mode
        if obs_type == "code_style_preference":
            # Learn code style
            for obs in observations:
                style_key = obs.get('context', {}).get('style_key')
                if style_key:
                    prefs.code_style[style_key] = obs['value']
                    # Update confidence
                    current = prefs.confidence.get(f"code_style.{style_key}", 0)
                    prefs.confidence[f"code_style.{style_key}"] = min(
                        1.0,
                        current + 0.1 * obs['strength']
                    )

        elif obs_type == "tool_preference":
            # Learn tool preferences
            tool_votes: Dict[str, float] = defaultdict(float)
            for obs in observations:
                tool_name = obs['value']
                tool_votes[tool_name] += obs['strength']

            # Pick most preferred tool
            if tool_votes:
                context_key = observations[0].get('context', {}).get('context_key', 'default')
                best_tool = max(tool_votes, key=tool_votes.get)
                prefs.preferred_tools[context_key] = best_tool
                prefs.confidence[f"tool.{context_key}"] = min(
                    1.0,
                    tool_votes[best_tool] / len(observations)
                )

        elif obs_type == "validation_acceptance":
            # Learn validation preferences
            accept_count = sum(1 for o in observations if o['value'] == 'accept')
            reject_count = len(observations) - accept_count

            if accept_count > reject_count * 2:
                # User often accepts - lower validation level
                prefs.validation_level = "minimal"
            elif reject_count > accept_count * 2:
                # User often rejects - raise validation level
                prefs.validation_level = "strict"

            prefs.confidence["validation_level"] = abs(
                accept_count - reject_count
            ) / len(observations)

    # Task history

    async def record_task_completion(
        self,
        user_id: str,
        task_id: str,
        task_type: str,
        success: bool,
        duration_ms: int = 0,
        retry_count: int = 0,
        tools_used: Optional[List[str]] = None,
        files_modified: Optional[List[str]] = None,
        validation_results: Optional[Dict[str, bool]] = None
    ) -> None:
        """Record task completion in history."""
        history = TaskHistory(
            task_id=task_id,
            task_type=task_type,
            risk_level="low",  # Would be passed from task context
            success=success,
            duration_ms=duration_ms,
            retry_count=retry_count,
            tools_used=tools_used or [],
            files_modified=files_modified or [],
            validation_results=validation_results or {},
            timestamp=datetime.utcnow()
        )

        # Store in Redis
        await self.redis.lpush(
            f"history:{user_id}",
            json.dumps({
                'task_id': history.task_id,
                'task_type': history.task_type,
                'risk_level': history.risk_level,
                'success': history.success,
                'duration_ms': history.duration_ms,
                'retry_count': history.retry_count,
                'tools_used': history.tools_used,
                'files_modified': history.files_modified,
                'validation_results': history.validation_results,
                'timestamp': history.timestamp.isoformat()
            })
        )

        # Trim to max
        await self.redis.ltrim(
            f"history:{user_id}",
            0,
            self.config.max_history_per_user - 1
        )

        # Record observation for learning
        await self.record_observation(
            user_id,
            "task_completion",
            success,
            signal_strength=1.0 if success else 0.5,
            context={
                'task_type': task_type,
                'retry_count': retry_count
            }
        )

    async def get_task_history(
        self,
        user_id: str,
        limit: int = 50,
        task_type: Optional[str] = None
    ) -> List[TaskHistory]:
        """Get user's task history."""
        # Check cache
        cache_key = f"{user_id}:{task_type}:{limit}"
        if cache_key in self._history_cache:
            return self._history_cache[cache_key]

        # Get from Redis
        raw_history = await self.redis.lrange(
            f"history:{user_id}",
            0,
            limit - 1
        )

        history = []
        for raw in raw_history:
            data = json.loads(raw)
            h = TaskHistory(
                task_id=data['task_id'],
                task_type=data['task_type'],
                risk_level=data['risk_level'],
                success=data['success'],
                duration_ms=data['duration_ms'],
                retry_count=data['retry_count'],
                tools_used=data['tools_used'],
                files_modified=data['files_modified'],
                validation_results=data['validation_results'],
                timestamp=datetime.fromisoformat(data['timestamp'])
            )

            if task_type is None or h.task_type == task_type:
                history.append(h)

        # Cache
        self._history_cache[cache_key] = history

        return history

    async def get_success_rate(
        self,
        user_id: str,
        task_type: Optional[str] = None
    ) -> float:
        """Get task success rate for user."""
        history = await self.get_task_history(user_id, limit=100, task_type=task_type)

        if not history:
            return 0.0

        success_count = sum(1 for h in history if h.success)
        return success_count / len(history)

    async def get_common_tools(
        self,
        user_id: str,
        task_type: Optional[str] = None,
        limit: int = 10
    ) -> List[Tuple[str, int]]:
        """Get most commonly used tools for user."""
        history = await self.get_task_history(user_id, limit=100, task_type=task_type)

        tool_counts: Dict[str, int] = defaultdict(int)
        for h in history:
            for tool in h.tools_used:
                tool_counts[tool] += 1

        sorted_tools = sorted(
            tool_counts.items(),
            key=lambda x: x[1],
            reverse=True
        )

        return sorted_tools[:limit]

    # Cross-session context

    async def store_session_summary(
        self,
        session_id: str,
        user_id: str,
        summary: str,
        key_files: List[str],
        key_decisions: List[str]
    ) -> None:
        """Store session summary for cross-session context."""
        data = {
            'session_id': session_id,
            'summary': summary,
            'key_files': key_files,
            'key_decisions': key_decisions,
            'timestamp': datetime.utcnow().isoformat()
        }

        await self.redis.lpush(
            f"session_summaries:{user_id}",
            json.dumps(data)
        )

        # Keep last 20 session summaries
        await self.redis.ltrim(f"session_summaries:{user_id}", 0, 19)

    async def get_session_summaries(
        self,
        user_id: str,
        limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Get recent session summaries for context."""
        raw = await self.redis.lrange(
            f"session_summaries:{user_id}",
            0,
            limit - 1
        )

        return [json.loads(r) for r in raw]

    async def get_relevant_context(
        self,
        user_id: str,
        current_files: List[str]
    ) -> Dict[str, Any]:
        """Get relevant cross-session context for current task."""
        summaries = await self.get_session_summaries(user_id, limit=10)

        # Find summaries with overlapping files
        relevant = []
        for summary in summaries:
            overlap = set(summary['key_files']) & set(current_files)
            if overlap:
                relevant.append({
                    **summary,
                    'relevance': len(overlap)
                })

        # Sort by relevance
        relevant.sort(key=lambda x: x['relevance'], reverse=True)

        return {
            'recent_summaries': summaries[:3],
            'relevant_summaries': relevant[:3]
        }

    # Cache management

    def clear_cache(self, user_id: Optional[str] = None) -> None:
        """Clear memory caches."""
        if user_id:
            self._preference_cache.pop(user_id, None)
            # Clear history cache entries for user
            keys_to_remove = [k for k in self._history_cache if k.startswith(f"{user_id}:")]
            for k in keys_to_remove:
                del self._history_cache[k]
        else:
            self._preference_cache.clear()
            self._history_cache.clear()


# Type alias for convenience
from typing import Tuple
