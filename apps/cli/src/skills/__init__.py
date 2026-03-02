"""Skills system for Glock CLI."""

from .base import Skill, SkillResult
from .registry import SkillRegistry
from .loader import SkillLoader
from .composer import (
    CompositeSkill,
    CompositeSkillBuilder,
    CompositeSkillResult,
    SkillInvoker,
    SkillStep,
    register_composite_skills,
)

__all__ = [
    "Skill",
    "SkillResult",
    "SkillRegistry",
    "SkillLoader",
    "CompositeSkill",
    "CompositeSkillBuilder",
    "CompositeSkillResult",
    "SkillInvoker",
    "SkillStep",
    "register_composite_skills",
]
