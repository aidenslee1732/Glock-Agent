"""Hierarchical configuration system.

Supports loading and merging configuration from multiple levels:
- Global: ~/.glock/GLOCK.md
- Project: ./GLOCK.md (at project root)
- Directory: ./.glock/config.md (per-directory overrides)

Configurations are merged with later/more-specific configs taking precedence.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .project_config import ProjectConfig, find_glock_config

logger = logging.getLogger(__name__)


@dataclass
class ConfigSource:
    """Represents a configuration source."""

    level: str  # "global", "project", "directory"
    path: Path
    config: ProjectConfig
    priority: int  # Higher priority = takes precedence


@dataclass
class HierarchicalConfig:
    """Merged hierarchical configuration."""

    # The final merged config
    merged: ProjectConfig

    # Individual config sources (for debugging/introspection)
    sources: list[ConfigSource] = field(default_factory=list)

    # Additional settings from .glock/ directory
    settings: dict[str, Any] = field(default_factory=dict)

    def to_system_prompt_section(self) -> str:
        """Generate system prompt section from merged config."""
        return self.merged.to_system_prompt_section()

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Get a setting value."""
        return self.settings.get(key, default)

    def source_summary(self) -> str:
        """Get a summary of configuration sources."""
        if not self.sources:
            return "No configuration sources"

        lines = ["Configuration sources:"]
        for source in self.sources:
            lines.append(f"  [{source.level}] {source.path}")
        return "\n".join(lines)


class ConfigHierarchy:
    """Loads and merges hierarchical configuration.

    Supports multiple configuration levels with proper precedence:
    1. Global config (~/.glock/GLOCK.md) - lowest priority
    2. Project config (./GLOCK.md or ./.glock/GLOCK.md) - medium priority
    3. Directory config (./.glock/config.md in current dir) - highest priority
    """

    def __init__(self, workspace: Optional[Path] = None):
        """Initialize configuration hierarchy.

        Args:
            workspace: Workspace path (defaults to cwd)
        """
        self.workspace = workspace or Path.cwd()

    def load_merged_config(self) -> HierarchicalConfig:
        """Load and merge configs from all levels.

        Returns:
            HierarchicalConfig with merged configuration
        """
        sources: list[ConfigSource] = []
        settings: dict[str, Any] = {}

        # Load global config (priority 0)
        global_config = self._load_global_config()
        if global_config:
            sources.append(ConfigSource(
                level="global",
                path=global_config[0],
                config=global_config[1],
                priority=0,
            ))
            settings.update(self._load_global_settings())

        # Load project config (priority 1)
        project_config = self._load_project_config()
        if project_config:
            sources.append(ConfigSource(
                level="project",
                path=project_config[0],
                config=project_config[1],
                priority=1,
            ))
            settings.update(self._load_project_settings())

        # Load directory config (priority 2)
        directory_config = self._load_directory_config()
        if directory_config:
            sources.append(ConfigSource(
                level="directory",
                path=directory_config[0],
                config=directory_config[1],
                priority=2,
            ))

        # Merge configs in priority order
        merged = ProjectConfig()
        for source in sorted(sources, key=lambda s: s.priority):
            merged = merged.merge_with(source.config)

        return HierarchicalConfig(
            merged=merged,
            sources=sources,
            settings=settings,
        )

    def _load_global_config(self) -> Optional[tuple[Path, ProjectConfig]]:
        """Load global configuration from ~/.glock/."""
        global_path = Path.home() / ".glock" / "GLOCK.md"

        if global_path.exists():
            try:
                content = global_path.read_text()
                config = ProjectConfig.from_markdown(content, str(global_path))
                logger.debug(f"Loaded global config from {global_path}")
                return (global_path, config)
            except Exception as e:
                logger.warning(f"Failed to load global config: {e}")

        return None

    def _load_global_settings(self) -> dict[str, Any]:
        """Load additional global settings from ~/.glock/settings.json."""
        settings_path = Path.home() / ".glock" / "settings.json"

        if settings_path.exists():
            try:
                with open(settings_path) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load global settings: {e}")

        return {}

    def _load_project_config(self) -> Optional[tuple[Path, ProjectConfig]]:
        """Load project configuration."""
        # Check for .glock/GLOCK.md first
        glock_dir_path = self.workspace / ".glock" / "GLOCK.md"
        if glock_dir_path.exists():
            try:
                content = glock_dir_path.read_text()
                config = ProjectConfig.from_markdown(content, str(glock_dir_path))
                logger.debug(f"Loaded project config from {glock_dir_path}")
                return (glock_dir_path, config)
            except Exception as e:
                logger.warning(f"Failed to load .glock/GLOCK.md: {e}")

        # Fall back to GLOCK.md in project root
        project_path = find_glock_config(self.workspace)
        if project_path:
            try:
                content = project_path.read_text()
                config = ProjectConfig.from_markdown(content, str(project_path))
                logger.debug(f"Loaded project config from {project_path}")
                return (project_path, config)
            except Exception as e:
                logger.warning(f"Failed to load project config: {e}")

        return None

    def _load_project_settings(self) -> dict[str, Any]:
        """Load project-specific settings."""
        settings_path = self.workspace / ".glock" / "settings.json"

        if settings_path.exists():
            try:
                with open(settings_path) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load project settings: {e}")

        return {}

    def _load_directory_config(self) -> Optional[tuple[Path, ProjectConfig]]:
        """Load directory-specific configuration."""
        # Check for .glock/config.md in current directory
        config_path = self.workspace / ".glock" / "config.md"

        if config_path.exists():
            try:
                content = config_path.read_text()
                config = ProjectConfig.from_markdown(content, str(config_path))
                logger.debug(f"Loaded directory config from {config_path}")
                return (config_path, config)
            except Exception as e:
                logger.warning(f"Failed to load directory config: {e}")

        return None

    def reload(self) -> HierarchicalConfig:
        """Reload all configuration from disk."""
        return self.load_merged_config()

    def get_effective_config(self, subdirectory: Optional[Path] = None) -> HierarchicalConfig:
        """Get effective configuration for a subdirectory.

        Args:
            subdirectory: Subdirectory path (relative to workspace)

        Returns:
            HierarchicalConfig for that directory
        """
        if subdirectory is None:
            return self.load_merged_config()

        # Create a temporary hierarchy for the subdirectory
        subdir_path = self.workspace / subdirectory
        if not subdir_path.exists():
            return self.load_merged_config()

        sub_hierarchy = ConfigHierarchy(workspace=subdir_path)
        return sub_hierarchy.load_merged_config()


def load_hierarchical_config(workspace: Optional[Path] = None) -> HierarchicalConfig:
    """Convenience function to load hierarchical configuration.

    Args:
        workspace: Workspace path

    Returns:
        Merged HierarchicalConfig
    """
    return ConfigHierarchy(workspace).load_merged_config()
