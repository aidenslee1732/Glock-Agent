"""Skill registry for managing available skills."""

from __future__ import annotations

import logging
from typing import Dict, Optional

from .base import Skill, SkillResult

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Registry for managing available skills.

    Skills are registered by name and can be looked up by name or alias.
    """

    def __init__(self):
        """Initialize the registry."""
        self._skills: Dict[str, Skill] = {}
        self._aliases: Dict[str, str] = {}  # alias -> skill name

    def register(self, skill: Skill) -> None:
        """Register a skill.

        Args:
            skill: Skill to register
        """
        self._skills[skill.name] = skill

        # Register aliases
        for alias in skill.aliases:
            self._aliases[alias.lower()] = skill.name

    def unregister(self, name: str) -> bool:
        """Unregister a skill.

        Args:
            name: Skill name

        Returns:
            True if skill was removed
        """
        if name in self._skills:
            skill = self._skills.pop(name)
            # Remove aliases
            for alias in skill.aliases:
                self._aliases.pop(alias.lower(), None)
            return True
        return False

    def get(self, name: str) -> Optional[Skill]:
        """Get a skill by name or alias.

        Args:
            name: Skill name or alias

        Returns:
            Skill if found, None otherwise
        """
        # Direct lookup
        if name in self._skills:
            return self._skills[name]

        # Alias lookup
        name_lower = name.lower()
        if name_lower in self._aliases:
            real_name = self._aliases[name_lower]
            return self._skills.get(real_name)

        # Check case-insensitive
        for skill_name in self._skills:
            if skill_name.lower() == name_lower:
                return self._skills[skill_name]

        return None

    def list_skills(self, category: Optional[str] = None) -> list[Skill]:
        """List all registered skills.

        Args:
            category: Optional category filter

        Returns:
            List of skills
        """
        skills = list(self._skills.values())

        if category:
            skills = [s for s in skills if s.category == category]

        return sorted(skills, key=lambda s: s.name)

    def list_categories(self) -> list[str]:
        """List all skill categories.

        Returns:
            List of category names
        """
        categories = set(s.category for s in self._skills.values())
        return sorted(categories)

    async def invoke(
        self,
        name: str,
        args: str = "",
        context: Optional[dict] = None,
    ) -> SkillResult:
        """Invoke a skill by name.

        Args:
            name: Skill name or alias
            args: Arguments string
            context: Execution context

        Returns:
            SkillResult
        """
        skill = self.get(name)
        if not skill:
            return SkillResult(
                status="failed",
                error=f"Unknown skill: {name}",
            )

        return await skill.invoke(args, context)
