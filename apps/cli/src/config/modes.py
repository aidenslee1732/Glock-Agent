"""Operational Modes - SMART/RUSH/DEEP execution strategies.

Provides Amp-like operational modes that adjust execution behavior:
- SMART: Balanced approach (default)
- RUSH: Fast, minimal council checks
- DEEP: Thorough analysis with full council
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class OperationalMode(str, Enum):
    """Operational modes for task execution."""

    SMART = "smart"  # Balanced (default) - adaptive council based on task
    RUSH = "rush"    # Fast - minimal council, quick responses
    DEEP = "deep"    # Thorough - full council, comprehensive analysis


@dataclass
class ModeConfig:
    """Configuration for an operational mode."""

    # Mode identifier
    mode: OperationalMode

    # Council settings
    council_enabled: bool = True
    council_timeout: float = 120.0
    council_perspectives: list[str] = field(default_factory=lambda: [
        "correctness", "security"
    ])
    council_strategy: str = "parallel"

    # Quality gate settings
    quality_gate_enabled: bool = True
    quality_gate_min_score: float = 60.0

    # Execution settings
    max_iterations: int = 10
    iteration_timeout: float = 300.0

    # Verification settings
    verification_level: str = "standard"  # none, minimal, standard, comprehensive

    # Model settings
    model_tier: str = "standard"  # fast, standard, advanced

    # Additional behavior flags
    auto_approve_simple: bool = False
    enable_caching: bool = True
    verbose_reasoning: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "mode": self.mode.value,
            "council_enabled": self.council_enabled,
            "council_timeout": self.council_timeout,
            "council_perspectives": self.council_perspectives,
            "council_strategy": self.council_strategy,
            "quality_gate_enabled": self.quality_gate_enabled,
            "quality_gate_min_score": self.quality_gate_min_score,
            "max_iterations": self.max_iterations,
            "iteration_timeout": self.iteration_timeout,
            "verification_level": self.verification_level,
            "model_tier": self.model_tier,
            "auto_approve_simple": self.auto_approve_simple,
            "enable_caching": self.enable_caching,
            "verbose_reasoning": self.verbose_reasoning,
        }


# Predefined mode configurations

SMART_MODE = ModeConfig(
    mode=OperationalMode.SMART,
    council_enabled=True,
    council_timeout=120.0,
    council_perspectives=["correctness", "security", "simplicity", "edge_cases"],
    council_strategy="adaptive",
    quality_gate_enabled=True,
    quality_gate_min_score=60.0,
    max_iterations=10,
    verification_level="standard",
    model_tier="standard",
    auto_approve_simple=True,
    enable_caching=True,
    verbose_reasoning=False,
)

RUSH_MODE = ModeConfig(
    mode=OperationalMode.RUSH,
    council_enabled=True,  # Still enabled but minimal
    council_timeout=30.0,
    council_perspectives=["correctness"],  # Only correctness
    council_strategy="parallel",
    quality_gate_enabled=False,  # Skip quality gate
    quality_gate_min_score=40.0,
    max_iterations=5,
    verification_level="minimal",
    model_tier="fast",
    auto_approve_simple=True,
    enable_caching=True,
    verbose_reasoning=False,
)

DEEP_MODE = ModeConfig(
    mode=OperationalMode.DEEP,
    council_enabled=True,
    council_timeout=300.0,
    council_perspectives=[
        "correctness", "security", "simplicity", "edge_cases",
        "performance", "maintainability", "data_integrity"
    ],
    council_strategy="tiered",
    quality_gate_enabled=True,
    quality_gate_min_score=75.0,
    max_iterations=20,
    verification_level="comprehensive",
    model_tier="advanced",
    auto_approve_simple=False,
    enable_caching=False,  # Fresh analysis each time
    verbose_reasoning=True,
)


def get_mode_config(mode: OperationalMode | str) -> ModeConfig:
    """Get configuration for a mode.

    Args:
        mode: Mode to get config for

    Returns:
        ModeConfig for the mode
    """
    if isinstance(mode, str):
        mode = OperationalMode(mode.lower())

    configs = {
        OperationalMode.SMART: SMART_MODE,
        OperationalMode.RUSH: RUSH_MODE,
        OperationalMode.DEEP: DEEP_MODE,
    }

    return configs.get(mode, SMART_MODE)


class ModeManager:
    """Manages operational modes for a session.

    Allows switching between modes and customizing mode settings.
    """

    def __init__(self, default_mode: OperationalMode = OperationalMode.SMART):
        """Initialize mode manager.

        Args:
            default_mode: Default operational mode
        """
        self._current_mode = default_mode
        self._custom_config: Optional[ModeConfig] = None
        self._mode_history: list[OperationalMode] = []

    @property
    def current_mode(self) -> OperationalMode:
        """Get current operational mode."""
        return self._current_mode

    @property
    def config(self) -> ModeConfig:
        """Get current mode configuration."""
        if self._custom_config:
            return self._custom_config
        return get_mode_config(self._current_mode)

    def set_mode(self, mode: OperationalMode | str) -> ModeConfig:
        """Set operational mode.

        Args:
            mode: New mode to use

        Returns:
            New mode configuration
        """
        if isinstance(mode, str):
            mode = OperationalMode(mode.lower())

        self._mode_history.append(self._current_mode)
        self._current_mode = mode
        self._custom_config = None  # Reset custom config

        logger.info(f"Switched to {mode.value} mode")
        return self.config

    def customize(self, **kwargs) -> ModeConfig:
        """Customize current mode configuration.

        Args:
            **kwargs: Configuration overrides

        Returns:
            Updated configuration
        """
        base_config = get_mode_config(self._current_mode)

        # Create custom config with overrides
        self._custom_config = ModeConfig(
            mode=self._current_mode,
            council_enabled=kwargs.get("council_enabled", base_config.council_enabled),
            council_timeout=kwargs.get("council_timeout", base_config.council_timeout),
            council_perspectives=kwargs.get("council_perspectives", base_config.council_perspectives),
            council_strategy=kwargs.get("council_strategy", base_config.council_strategy),
            quality_gate_enabled=kwargs.get("quality_gate_enabled", base_config.quality_gate_enabled),
            quality_gate_min_score=kwargs.get("quality_gate_min_score", base_config.quality_gate_min_score),
            max_iterations=kwargs.get("max_iterations", base_config.max_iterations),
            iteration_timeout=kwargs.get("iteration_timeout", base_config.iteration_timeout),
            verification_level=kwargs.get("verification_level", base_config.verification_level),
            model_tier=kwargs.get("model_tier", base_config.model_tier),
            auto_approve_simple=kwargs.get("auto_approve_simple", base_config.auto_approve_simple),
            enable_caching=kwargs.get("enable_caching", base_config.enable_caching),
            verbose_reasoning=kwargs.get("verbose_reasoning", base_config.verbose_reasoning),
        )

        return self._custom_config

    def reset(self) -> ModeConfig:
        """Reset to default mode and clear customizations."""
        self._current_mode = OperationalMode.SMART
        self._custom_config = None
        return self.config

    def previous_mode(self) -> Optional[OperationalMode]:
        """Get previous mode if any."""
        if self._mode_history:
            return self._mode_history[-1]
        return None

    def restore_previous(self) -> ModeConfig:
        """Restore previous mode.

        Returns:
            Previous mode configuration
        """
        if self._mode_history:
            previous = self._mode_history.pop()
            self._current_mode = previous
            self._custom_config = None
            return self.config

        return self.config

    def should_run_council(self, task_complexity: str = "normal") -> bool:
        """Determine if council should run based on mode and task.

        Args:
            task_complexity: "trivial", "simple", "normal", "complex"

        Returns:
            Whether to run council
        """
        config = self.config

        if not config.council_enabled:
            return False

        # Auto-approve simple tasks in some modes
        if config.auto_approve_simple and task_complexity in ("trivial", "simple"):
            return False

        return True

    def get_council_perspectives(self) -> list[str]:
        """Get perspectives to use for current mode."""
        return self.config.council_perspectives

    def get_model_tier(self) -> str:
        """Get model tier for current mode."""
        return self.config.model_tier

    def status(self) -> dict[str, Any]:
        """Get mode manager status."""
        return {
            "current_mode": self._current_mode.value,
            "config": self.config.to_dict(),
            "has_custom_config": self._custom_config is not None,
            "history_length": len(self._mode_history),
        }


# Convenience functions


def smart_mode() -> ModeConfig:
    """Get SMART mode configuration."""
    return SMART_MODE


def rush_mode() -> ModeConfig:
    """Get RUSH mode configuration."""
    return RUSH_MODE


def deep_mode() -> ModeConfig:
    """Get DEEP mode configuration."""
    return DEEP_MODE
