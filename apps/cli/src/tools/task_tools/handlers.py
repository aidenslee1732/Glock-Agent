"""Task tool handlers."""

from __future__ import annotations

from typing import Any, Optional

from ...tasks import TaskManager, TaskStore, BackgroundTaskRunner


# Global instances (initialized by broker)
_task_manager: Optional[TaskManager] = None
_background_runner: Optional[BackgroundTaskRunner] = None


def init_task_tools(
    task_manager: Optional[TaskManager] = None,
    background_runner: Optional[BackgroundTaskRunner] = None,
) -> None:
    """Initialize task tools with manager instances.

    Args:
        task_manager: TaskManager instance
        background_runner: BackgroundTaskRunner instance
    """
    global _task_manager, _background_runner

    if task_manager:
        _task_manager = task_manager
    else:
        store = TaskStore()
        _task_manager = TaskManager(store)

    if background_runner:
        _background_runner = background_runner
    else:
        _background_runner = BackgroundTaskRunner(store=_task_manager.store)


def _get_manager() -> TaskManager:
    """Get or initialize the task manager."""
    global _task_manager
    if _task_manager is None:
        init_task_tools()
    return _task_manager


def _get_runner() -> BackgroundTaskRunner:
    """Get or initialize the background runner."""
    global _background_runner
    if _background_runner is None:
        init_task_tools()
    return _background_runner


def set_background_runner(runner: BackgroundTaskRunner) -> None:
    """Set background runner after initialization."""
    global _background_runner
    _background_runner = runner


async def task_create_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Create a new task.

    Args:
        args: Dictionary containing:
            - subject: Brief title for the task (required)
            - description: Detailed description (required)
            - activeForm: Present continuous form for spinner (optional)
            - metadata: Arbitrary metadata dict (optional)

    Returns:
        Dictionary with task ID and confirmation
    """
    manager = _get_manager()

    subject = args.get("subject")
    description = args.get("description")

    if not subject:
        return {
            "status": "error",
            "error": "subject is required",
        }

    if not description:
        return {
            "status": "error",
            "error": "description is required",
        }

    task = manager.create_task(
        subject=subject,
        description=description,
        active_form=args.get("activeForm"),
        metadata=args.get("metadata"),
    )

    return {
        "status": "success",
        "message": f"Task #{task.id} created successfully: {task.subject}",
        "task_id": task.id,
    }


async def task_list_handler(args: dict[str, Any]) -> dict[str, Any]:
    """List all tasks.

    Args:
        args: Dictionary containing (all optional):
            - status: Filter by status
            - owner: Filter by owner

    Returns:
        Dictionary with task list
    """
    manager = _get_manager()

    tasks = manager.list_tasks(
        status=args.get("status"),
        owner=args.get("owner"),
    )

    task_list = []
    for task in tasks:
        task_info = {
            "id": task.id,
            "subject": task.subject,
            "status": task.status.value,
        }

        if task.owner:
            task_info["owner"] = task.owner

        if task.blocked_by:
            # Filter to only show incomplete blockers
            incomplete_blockers = []
            for blocker_id in task.blocked_by:
                blocker = manager.get_task(blocker_id)
                if blocker and blocker.status.value != "completed":
                    incomplete_blockers.append(blocker_id)

            if incomplete_blockers:
                task_info["blockedBy"] = incomplete_blockers

        task_list.append(task_info)

    return {
        "status": "success",
        "tasks": task_list,
        "total": len(task_list),
    }


async def task_get_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Get task details by ID.

    Args:
        args: Dictionary containing:
            - taskId: Task ID to retrieve (required)

    Returns:
        Dictionary with full task details
    """
    manager = _get_manager()

    task_id = args.get("taskId")
    if not task_id:
        return {
            "status": "error",
            "error": "taskId is required",
        }

    task = manager.get_task(task_id)
    if not task:
        return {
            "status": "error",
            "error": f"Task not found: {task_id}",
        }

    result = {
        "status": "success",
        "task": {
            "id": task.id,
            "subject": task.subject,
            "description": task.description,
            "status": task.status.value,
        },
    }

    if task.owner:
        result["task"]["owner"] = task.owner

    if task.active_form:
        result["task"]["activeForm"] = task.active_form

    if task.blocks:
        result["task"]["blocks"] = task.blocks

    if task.blocked_by:
        result["task"]["blockedBy"] = task.blocked_by

    if task.metadata:
        result["task"]["metadata"] = task.metadata

    return result


async def task_update_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Update a task.

    Args:
        args: Dictionary containing:
            - taskId: Task ID to update (required)
            - status: New status ("pending", "in_progress", "completed", "deleted")
            - subject: New subject
            - description: New description
            - activeForm: New active form text
            - owner: New owner
            - metadata: Metadata to merge (set key to null to delete)
            - addBlocks: Task IDs to add to blocks list
            - addBlockedBy: Task IDs to add to blocked_by list

    Returns:
        Dictionary with updated task
    """
    manager = _get_manager()

    task_id = args.get("taskId")
    if not task_id:
        return {
            "status": "error",
            "error": "taskId is required",
        }

    # Check task exists
    existing = manager.get_task(task_id)
    if not existing:
        return {
            "status": "error",
            "error": f"Task not found: {task_id}",
        }

    # Perform update
    task = manager.update_task(
        task_id=task_id,
        status=args.get("status"),
        subject=args.get("subject"),
        description=args.get("description"),
        active_form=args.get("activeForm"),
        owner=args.get("owner"),
        metadata=args.get("metadata"),
        add_blocks=args.get("addBlocks"),
        add_blocked_by=args.get("addBlockedBy"),
    )

    if not task:
        return {
            "status": "error",
            "error": f"Failed to update task: {task_id}",
        }

    # Build response
    updates = []
    if args.get("status"):
        updates.append(f"status={args['status']}")
    if args.get("subject"):
        updates.append("subject")
    if args.get("description"):
        updates.append("description")
    if args.get("activeForm"):
        updates.append("activeForm")
    if args.get("owner"):
        updates.append(f"owner={args['owner']}")
    if args.get("addBlocks"):
        updates.append(f"blocks+={args['addBlocks']}")
    if args.get("addBlockedBy"):
        updates.append(f"blockedBy+={args['addBlockedBy']}")

    return {
        "status": "success",
        "message": f"Updated task #{task_id} {', '.join(updates)}" if updates else f"Updated task #{task_id}",
        "task_id": task_id,
    }


async def task_output_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Get output from a background task.

    Args:
        args: Dictionary containing:
            - task_id: Background task ID (required)
            - block: Wait for completion (default True)
            - timeout: Max wait time in ms (default 30000)

    Returns:
        Dictionary with task output
    """
    runner = _get_runner()

    task_id = args.get("task_id")
    if not task_id:
        return {
            "status": "error",
            "error": "task_id is required",
        }

    block = args.get("block", True)
    timeout_ms = args.get("timeout", 30000)
    timeout_sec = timeout_ms / 1000

    result = await runner.get_output(
        task_id=task_id,
        block=block,
        timeout=timeout_sec,
    )

    return result


async def task_stop_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Stop a running background task.

    Args:
        args: Dictionary containing:
            - task_id: Background task ID (required)

    Returns:
        Dictionary with status
    """
    runner = _get_runner()

    task_id = args.get("task_id")
    if not task_id:
        return {
            "status": "error",
            "error": "task_id is required",
        }

    result = await runner.stop_task(task_id)
    return result
