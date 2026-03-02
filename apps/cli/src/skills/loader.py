"""Skill loader for discovering and loading skills."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Optional

from .base import Skill
from .registry import SkillRegistry
from .composer import register_composite_skills

logger = logging.getLogger(__name__)


class SkillLoader:
    """Loads skills from built-in and custom locations."""

    def __init__(self, registry: Optional[SkillRegistry] = None):
        """Initialize the loader.

        Args:
            registry: SkillRegistry to load into
        """
        self.registry = registry or SkillRegistry()

    def load_builtin_skills(self) -> int:
        """Load all built-in skills.

        Returns:
            Number of skills loaded
        """
        from .builtin import commit, review_pr, create_pr, remember, review, security_scan

        loaded = 0

        # Commit skill
        if hasattr(commit, 'get_skill'):
            self.registry.register(commit.get_skill())
            loaded += 1

        # Review PR skill
        if hasattr(review_pr, 'get_skill'):
            self.registry.register(review_pr.get_skill())
            loaded += 1

        # Create PR skill
        if hasattr(create_pr, 'get_skill'):
            self.registry.register(create_pr.get_skill())
            loaded += 1

        # Remember skill
        if hasattr(remember, 'get_skill'):
            self.registry.register(remember.get_skill())
            loaded += 1

        # Review skill
        if hasattr(review, 'get_skill'):
            self.registry.register(review.get_skill())
            loaded += 1

        # Security scan skill
        if hasattr(security_scan, 'get_skill'):
            self.registry.register(security_scan.get_skill())
            loaded += 1

        logger.info(f"Loaded {loaded} built-in skills")
        return loaded

    def load_composite_skills(self) -> int:
        """Load composite skills that chain other skills.

        Returns:
            Number of composite skills loaded
        """
        return register_composite_skills(self.registry)

    def load_custom_skills(self, skills_dir: Path) -> int:
        """Load custom skills from a directory.

        Args:
            skills_dir: Directory containing skill modules

        Returns:
            Number of skills loaded
        """
        if not skills_dir.exists():
            return 0

        loaded = 0

        for skill_file in skills_dir.glob("*.py"):
            if skill_file.name.startswith("_"):
                continue

            try:
                # Import the module
                module_name = skill_file.stem
                spec = importlib.util.spec_from_file_location(
                    f"custom_skill_{module_name}",
                    skill_file,
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # Look for get_skill() function
                if hasattr(module, 'get_skill'):
                    skill = module.get_skill()
                    if isinstance(skill, Skill):
                        self.registry.register(skill)
                        loaded += 1
                        logger.info(f"Loaded custom skill: {skill.name}")

            except Exception as e:
                logger.warning(f"Failed to load skill from {skill_file}: {e}")

        return loaded

    def get_registry(self) -> SkillRegistry:
        """Get the skill registry.

        Returns:
            SkillRegistry with loaded skills
        """
        return self.registry
