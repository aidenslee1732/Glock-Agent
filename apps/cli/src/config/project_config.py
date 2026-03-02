"""GLOCK.md project configuration parser.

Detects and parses GLOCK.md files in project root to load project-specific
configuration. Supports global (~/.glock/GLOCK.md) with project override.

GLOCK.md Format:
```markdown
## Coding Standards
- Use type hints for all function signatures

## Preferred Libraries
- pytest for testing

## Architecture Decisions
- Use repository pattern for data access

## Review Checklist
- [ ] Tests cover edge cases

## Custom Instructions
Always include docstrings.

## Forbidden Patterns
- eval()
- exec()
```
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ProjectConfig:
    """Project configuration from GLOCK.md."""

    # Coding standards and guidelines
    coding_standards: str = ""

    # Preferred libraries for various tasks
    preferred_libraries: list[str] = field(default_factory=list)

    # Architecture decisions and patterns
    architecture_decisions: str = ""

    # Review checklist items
    review_checklist: list[str] = field(default_factory=list)

    # Custom instructions for the AI
    custom_instructions: str = ""

    # Patterns that should never be used
    forbidden_patterns: list[str] = field(default_factory=list)

    # Source file path
    source_path: Optional[str] = None

    @classmethod
    def from_markdown(cls, content: str, source_path: Optional[str] = None) -> "ProjectConfig":
        """Parse GLOCK.md content into structured config.

        Args:
            content: Markdown content from GLOCK.md
            source_path: Path to the source file

        Returns:
            Parsed ProjectConfig
        """
        config = cls(source_path=source_path)

        # Parse sections using regex
        sections = _parse_markdown_sections(content)

        for title, body in sections.items():
            title_lower = title.lower().strip()

            if "coding standard" in title_lower:
                config.coding_standards = body.strip()

            elif "preferred librar" in title_lower:
                config.preferred_libraries = _parse_list_items(body)

            elif "architecture" in title_lower or "decision" in title_lower:
                config.architecture_decisions = body.strip()

            elif "review checklist" in title_lower or "checklist" in title_lower:
                config.review_checklist = _parse_checklist_items(body)

            elif "custom instruction" in title_lower or "instruction" in title_lower:
                config.custom_instructions = body.strip()

            elif "forbidden" in title_lower or "disallowed" in title_lower:
                config.forbidden_patterns = _parse_list_items(body)

        return config

    def merge_with(self, other: "ProjectConfig") -> "ProjectConfig":
        """Merge another config into this one (other takes precedence).

        Args:
            other: Config to merge (takes precedence)

        Returns:
            New merged ProjectConfig
        """
        return ProjectConfig(
            coding_standards=other.coding_standards or self.coding_standards,
            preferred_libraries=(
                other.preferred_libraries
                if other.preferred_libraries
                else self.preferred_libraries
            ),
            architecture_decisions=(
                other.architecture_decisions or self.architecture_decisions
            ),
            review_checklist=(
                other.review_checklist if other.review_checklist else self.review_checklist
            ),
            custom_instructions=(
                other.custom_instructions or self.custom_instructions
            ),
            forbidden_patterns=list(
                set(self.forbidden_patterns) | set(other.forbidden_patterns)
            ),
            source_path=other.source_path or self.source_path,
        )

    def to_system_prompt_section(self) -> str:
        """Generate system prompt section from config.

        Returns:
            Formatted string for inclusion in system prompt
        """
        parts: list[str] = []

        if self.coding_standards:
            parts.append(f"## Coding Standards\n{self.coding_standards}")

        if self.preferred_libraries:
            libs = "\n".join(f"- {lib}" for lib in self.preferred_libraries)
            parts.append(f"## Preferred Libraries\n{libs}")

        if self.architecture_decisions:
            parts.append(f"## Architecture Decisions\n{self.architecture_decisions}")

        if self.review_checklist:
            items = "\n".join(f"- [ ] {item}" for item in self.review_checklist)
            parts.append(f"## Review Checklist\n{items}")

        if self.custom_instructions:
            parts.append(f"## Custom Instructions\n{self.custom_instructions}")

        if self.forbidden_patterns:
            patterns = "\n".join(f"- {p}" for p in self.forbidden_patterns)
            parts.append(f"## Forbidden Patterns (NEVER use these)\n{patterns}")

        if not parts:
            return ""

        return "# Project Configuration\n\n" + "\n\n".join(parts)

    def is_empty(self) -> bool:
        """Check if config has any meaningful content."""
        return not any([
            self.coding_standards,
            self.preferred_libraries,
            self.architecture_decisions,
            self.review_checklist,
            self.custom_instructions,
            self.forbidden_patterns,
        ])


def _parse_markdown_sections(content: str) -> dict[str, str]:
    """Parse markdown into sections by headers.

    Args:
        content: Markdown content

    Returns:
        Dict mapping header titles to section content
    """
    sections: dict[str, str] = {}

    # Match ## headers
    header_pattern = re.compile(r'^##\s+(.+)$', re.MULTILINE)
    matches = list(header_pattern.finditer(content))

    for i, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()

        # Find the end of this section
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(content)

        body = content[start:end].strip()
        sections[title] = body

    return sections


def _parse_list_items(text: str) -> list[str]:
    """Parse markdown list items.

    Args:
        text: Text containing markdown lists

    Returns:
        List of item strings
    """
    items: list[str] = []

    for line in text.split("\n"):
        line = line.strip()
        # Match - item or * item
        if line.startswith(("- ", "* ")):
            item = line[2:].strip()
            if item:
                items.append(item)

    return items


def _parse_checklist_items(text: str) -> list[str]:
    """Parse markdown checklist items.

    Args:
        text: Text containing markdown checklists

    Returns:
        List of checklist item strings (without checkbox)
    """
    items: list[str] = []

    for line in text.split("\n"):
        line = line.strip()
        # Match - [ ] item or - [x] item
        match = re.match(r'^[-*]\s*\[.\]\s*(.+)$', line)
        if match:
            item = match.group(1).strip()
            if item:
                items.append(item)
        # Also accept plain list items
        elif line.startswith(("- ", "* ")):
            item = line[2:].strip()
            if item:
                items.append(item)

    return items


def find_glock_config(workspace_path: Path) -> Optional[Path]:
    """Find GLOCK.md file in workspace or parent directories.

    Searches from workspace up to root, returning first GLOCK.md found.

    Args:
        workspace_path: Starting path to search from

    Returns:
        Path to GLOCK.md if found, None otherwise
    """
    current = workspace_path.resolve()

    while current != current.parent:
        glock_path = current / "GLOCK.md"
        if glock_path.exists():
            return glock_path
        current = current.parent

    return None


def load_project_config(workspace_path: Optional[Path] = None) -> ProjectConfig:
    """Load project configuration from GLOCK.md files.

    Loads global config from ~/.glock/GLOCK.md, then merges with
    project-specific config if found.

    Args:
        workspace_path: Project workspace path

    Returns:
        Merged ProjectConfig
    """
    config = ProjectConfig()

    # Load global config
    global_path = Path.home() / ".glock" / "GLOCK.md"
    if global_path.exists():
        try:
            content = global_path.read_text()
            global_config = ProjectConfig.from_markdown(content, str(global_path))
            config = config.merge_with(global_config)
            logger.info(f"Loaded global GLOCK.md from {global_path}")
        except Exception as e:
            logger.warning(f"Failed to load global GLOCK.md: {e}")

    # Load project config
    if workspace_path:
        project_path = find_glock_config(workspace_path)
        if project_path:
            try:
                content = project_path.read_text()
                project_config = ProjectConfig.from_markdown(content, str(project_path))
                config = config.merge_with(project_config)
                logger.info(f"Loaded project GLOCK.md from {project_path}")
            except Exception as e:
                logger.warning(f"Failed to load project GLOCK.md: {e}")

    return config
