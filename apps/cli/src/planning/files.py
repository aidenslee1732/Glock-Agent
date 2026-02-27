"""Plan file management."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import uuid


@dataclass
class Plan:
    """Represents a plan document.

    Attributes:
        id: Unique plan identifier
        title: Plan title
        content: Plan content (markdown)
        status: "draft", "pending_approval", "approved", "rejected", "executing", "completed"
        created_at: When the plan was created
        updated_at: When the plan was last modified
        approved_at: When the plan was approved (if approved)
        metadata: Additional metadata
    """
    id: str
    title: str
    content: str
    status: str = "draft"
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    approved_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Plan":
        """Create from dictionary."""
        return cls(
            id=data["id"],
            title=data["title"],
            content=data["content"],
            status=data.get("status", "draft"),
            created_at=datetime.fromisoformat(data["created_at"]) if isinstance(data.get("created_at"), str) else datetime.utcnow(),
            updated_at=datetime.fromisoformat(data["updated_at"]) if isinstance(data.get("updated_at"), str) else datetime.utcnow(),
            approved_at=datetime.fromisoformat(data["approved_at"]) if data.get("approved_at") else None,
            metadata=data.get("metadata", {}),
        )


class PlanFileManager:
    """Manages plan files.

    Plans are stored in ~/.glock/plans/ as markdown files with metadata.
    """

    def __init__(self, plans_dir: Optional[str] = None):
        """Initialize the manager.

        Args:
            plans_dir: Directory for plan files. Defaults to ~/.glock/plans/
        """
        if plans_dir:
            self.plans_dir = Path(plans_dir)
        else:
            self.plans_dir = Path.home() / ".glock" / "plans"

        self.plans_dir.mkdir(parents=True, exist_ok=True)
        self._metadata_file = self.plans_dir / ".metadata.json"

    def _load_metadata(self) -> dict[str, dict]:
        """Load metadata for all plans."""
        if not self._metadata_file.exists():
            return {}

        try:
            with open(self._metadata_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save_metadata(self, metadata: dict[str, dict]) -> None:
        """Save metadata for all plans."""
        with open(self._metadata_file, "w") as f:
            json.dump(metadata, f, indent=2, default=str)

    def create_plan(self, title: str, content: str = "") -> Plan:
        """Create a new plan.

        Args:
            title: Plan title
            content: Initial content

        Returns:
            Created plan
        """
        plan_id = f"plan_{uuid.uuid4().hex[:8]}"
        plan = Plan(
            id=plan_id,
            title=title,
            content=content,
        )

        # Save plan file
        plan_file = self.plans_dir / f"{plan_id}.md"
        plan_file.write_text(self._format_plan_file(plan))

        # Update metadata
        metadata = self._load_metadata()
        metadata[plan_id] = plan.to_dict()
        self._save_metadata(metadata)

        return plan

    def get_plan(self, plan_id: str) -> Optional[Plan]:
        """Get a plan by ID.

        Args:
            plan_id: Plan ID

        Returns:
            Plan if found, None otherwise
        """
        metadata = self._load_metadata()
        if plan_id not in metadata:
            return None

        plan_data = metadata[plan_id]

        # Read content from file
        plan_file = self.plans_dir / f"{plan_id}.md"
        if plan_file.exists():
            content = self._parse_plan_file(plan_file.read_text())
            plan_data["content"] = content

        return Plan.from_dict(plan_data)

    def update_plan(self, plan: Plan) -> Plan:
        """Update an existing plan.

        Args:
            plan: Plan with updated values

        Returns:
            Updated plan
        """
        plan.updated_at = datetime.utcnow()

        # Save plan file
        plan_file = self.plans_dir / f"{plan.id}.md"
        plan_file.write_text(self._format_plan_file(plan))

        # Update metadata
        metadata = self._load_metadata()
        metadata[plan.id] = plan.to_dict()
        self._save_metadata(metadata)

        return plan

    def delete_plan(self, plan_id: str) -> bool:
        """Delete a plan.

        Args:
            plan_id: Plan ID to delete

        Returns:
            True if deleted
        """
        # Remove file
        plan_file = self.plans_dir / f"{plan_id}.md"
        if plan_file.exists():
            plan_file.unlink()

        # Remove metadata
        metadata = self._load_metadata()
        if plan_id in metadata:
            del metadata[plan_id]
            self._save_metadata(metadata)
            return True

        return False

    def list_plans(self, status: Optional[str] = None) -> list[Plan]:
        """List all plans.

        Args:
            status: Optional status filter

        Returns:
            List of plans
        """
        metadata = self._load_metadata()
        plans = []

        for plan_id, plan_data in metadata.items():
            if status and plan_data.get("status") != status:
                continue
            plans.append(Plan.from_dict(plan_data))

        return sorted(plans, key=lambda p: p.updated_at, reverse=True)

    def get_current_plan(self) -> Optional[Plan]:
        """Get the current active plan (draft or pending_approval).

        Returns:
            Current plan if one exists
        """
        plans = self.list_plans()
        for plan in plans:
            if plan.status in ("draft", "pending_approval"):
                return plan
        return None

    def get_plan_file_path(self, plan_id: str) -> Path:
        """Get the file path for a plan.

        Args:
            plan_id: Plan ID

        Returns:
            Path to plan file
        """
        return self.plans_dir / f"{plan_id}.md"

    def _format_plan_file(self, plan: Plan) -> str:
        """Format a plan as a markdown file with frontmatter.

        Args:
            plan: Plan to format

        Returns:
            Formatted markdown content
        """
        lines = [
            "---",
            f"title: {plan.title}",
            f"status: {plan.status}",
            f"created: {plan.created_at.isoformat()}",
            f"updated: {plan.updated_at.isoformat()}",
            "---",
            "",
            plan.content,
        ]
        return "\n".join(lines)

    def _parse_plan_file(self, content: str) -> str:
        """Parse a plan file and extract content.

        Args:
            content: File content

        Returns:
            Plan content (without frontmatter)
        """
        lines = content.split("\n")

        # Skip frontmatter
        if lines and lines[0].strip() == "---":
            end_idx = 1
            for i, line in enumerate(lines[1:], 1):
                if line.strip() == "---":
                    end_idx = i + 1
                    break
            lines = lines[end_idx:]

        # Skip leading empty lines
        while lines and not lines[0].strip():
            lines = lines[1:]

        return "\n".join(lines)
