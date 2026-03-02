"""CI/CD Integration for Glock.

Phase 3 Feature 3.4: CI/CD integration.

Provides:
- GitHub Actions workflow generation and modification
- Pipeline status monitoring via GitHub API
- Test result parsing from CI logs
- Deployment hook support
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

logger = logging.getLogger(__name__)


class WorkflowStatus(str, Enum):
    """CI workflow run status."""
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    WAITING = "waiting"
    REQUESTED = "requested"
    PENDING = "pending"


class WorkflowConclusion(str, Enum):
    """CI workflow run conclusion."""
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    TIMED_OUT = "timed_out"
    ACTION_REQUIRED = "action_required"
    NEUTRAL = "neutral"


@dataclass
class WorkflowRun:
    """A CI workflow run."""
    id: int
    name: str
    workflow_id: int
    status: WorkflowStatus
    conclusion: Optional[WorkflowConclusion]
    branch: str
    commit_sha: str
    html_url: str
    created_at: datetime
    updated_at: datetime
    run_number: int
    jobs_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "conclusion": self.conclusion.value if self.conclusion else None,
            "branch": self.branch,
            "commit_sha": self.commit_sha[:8],
            "url": self.html_url,
            "run_number": self.run_number,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class WorkflowJob:
    """A job within a workflow run."""
    id: int
    name: str
    status: str
    conclusion: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    steps: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "conclusion": self.conclusion,
            "steps": self.steps,
        }


@dataclass
class TestResult:
    """Parsed test result from CI logs."""
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    duration_seconds: float = 0
    failed_tests: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
            "duration_seconds": self.duration_seconds,
            "failed_tests": self.failed_tests[:20],  # Limit
            "success_rate": self.passed / self.total if self.total > 0 else 0,
        }


class GitHubActionsClient:
    """Client for GitHub Actions API.

    Uses GitHub CLI (gh) or direct API calls.
    """

    API_BASE = "https://api.github.com"

    def __init__(
        self,
        owner: str,
        repo: str,
        token: Optional[str] = None,
    ):
        """Initialize GitHub Actions client.

        Args:
            owner: Repository owner
            repo: Repository name
            token: GitHub token (uses GH_TOKEN env if not provided)
        """
        self.owner = owner
        self.repo = repo
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    async def list_workflow_runs(
        self,
        branch: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 10,
    ) -> list[WorkflowRun]:
        """List recent workflow runs.

        Args:
            branch: Filter by branch
            status: Filter by status
            limit: Maximum runs to return

        Returns:
            List of workflow runs
        """
        if not AIOHTTP_AVAILABLE:
            logger.warning("aiohttp not available for GitHub API")
            return []

        url = f"{self.API_BASE}/repos/{self.owner}/{self.repo}/actions/runs"
        params = {"per_page": limit}
        if branch:
            params["branch"] = branch
        if status:
            params["status"] = status

        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status != 200:
                        logger.warning(f"GitHub API error: {resp.status}")
                        return []

                    data = await resp.json()
                    runs = []

                    for run in data.get("workflow_runs", []):
                        runs.append(WorkflowRun(
                            id=run["id"],
                            name=run["name"],
                            workflow_id=run["workflow_id"],
                            status=WorkflowStatus(run["status"]),
                            conclusion=WorkflowConclusion(run["conclusion"]) if run.get("conclusion") else None,
                            branch=run["head_branch"],
                            commit_sha=run["head_sha"],
                            html_url=run["html_url"],
                            created_at=datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")),
                            updated_at=datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00")),
                            run_number=run["run_number"],
                            jobs_url=run["jobs_url"],
                        ))

                    return runs

        except Exception as e:
            logger.error(f"Failed to list workflow runs: {e}")
            return []

    async def get_workflow_run(self, run_id: int) -> Optional[WorkflowRun]:
        """Get a specific workflow run.

        Args:
            run_id: Workflow run ID

        Returns:
            WorkflowRun or None
        """
        if not AIOHTTP_AVAILABLE:
            return None

        url = f"{self.API_BASE}/repos/{self.owner}/{self.repo}/actions/runs/{run_id}"
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        return None

                    run = await resp.json()
                    return WorkflowRun(
                        id=run["id"],
                        name=run["name"],
                        workflow_id=run["workflow_id"],
                        status=WorkflowStatus(run["status"]),
                        conclusion=WorkflowConclusion(run["conclusion"]) if run.get("conclusion") else None,
                        branch=run["head_branch"],
                        commit_sha=run["head_sha"],
                        html_url=run["html_url"],
                        created_at=datetime.fromisoformat(run["created_at"].replace("Z", "+00:00")),
                        updated_at=datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00")),
                        run_number=run["run_number"],
                        jobs_url=run["jobs_url"],
                    )

        except Exception as e:
            logger.error(f"Failed to get workflow run: {e}")
            return None

    async def get_run_jobs(self, run_id: int) -> list[WorkflowJob]:
        """Get jobs for a workflow run.

        Args:
            run_id: Workflow run ID

        Returns:
            List of jobs
        """
        if not AIOHTTP_AVAILABLE:
            return []

        url = f"{self.API_BASE}/repos/{self.owner}/{self.repo}/actions/runs/{run_id}/jobs"
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        return []

                    data = await resp.json()
                    jobs = []

                    for job in data.get("jobs", []):
                        jobs.append(WorkflowJob(
                            id=job["id"],
                            name=job["name"],
                            status=job["status"],
                            conclusion=job.get("conclusion"),
                            started_at=datetime.fromisoformat(job["started_at"].replace("Z", "+00:00")) if job.get("started_at") else None,
                            completed_at=datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00")) if job.get("completed_at") else None,
                            steps=[
                                {
                                    "name": step["name"],
                                    "status": step["status"],
                                    "conclusion": step.get("conclusion"),
                                }
                                for step in job.get("steps", [])
                            ],
                        ))

                    return jobs

        except Exception as e:
            logger.error(f"Failed to get run jobs: {e}")
            return []

    async def get_run_logs(self, run_id: int) -> Optional[str]:
        """Get logs for a workflow run.

        Args:
            run_id: Workflow run ID

        Returns:
            Log content or None
        """
        # Use gh CLI for logs (easier than API which returns ZIP)
        try:
            process = await asyncio.create_subprocess_exec(
                "gh", "run", "view", str(run_id),
                "--repo", f"{self.owner}/{self.repo}",
                "--log",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                return stdout.decode()
            else:
                logger.warning(f"gh run view failed: {stderr.decode()}")
                return None

        except FileNotFoundError:
            logger.warning("gh CLI not found")
            return None
        except Exception as e:
            logger.error(f"Failed to get run logs: {e}")
            return None

    async def trigger_workflow(
        self,
        workflow_id: str,
        ref: str = "main",
        inputs: Optional[dict[str, str]] = None,
    ) -> bool:
        """Trigger a workflow dispatch.

        Args:
            workflow_id: Workflow file name (e.g., "ci.yml")
            ref: Git ref to run on
            inputs: Workflow inputs

        Returns:
            True if triggered successfully
        """
        if not AIOHTTP_AVAILABLE:
            return False

        url = f"{self.API_BASE}/repos/{self.owner}/{self.repo}/actions/workflows/{workflow_id}/dispatches"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
        }
        payload = {"ref": ref}
        if inputs:
            payload["inputs"] = inputs

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    return resp.status == 204

        except Exception as e:
            logger.error(f"Failed to trigger workflow: {e}")
            return False


class WorkflowGenerator:
    """Generate GitHub Actions workflow files."""

    TEMPLATES = {
        "python": {
            "name": "Python CI",
            "on": {
                "push": {"branches": ["main", "master"]},
                "pull_request": {"branches": ["main", "master"]},
            },
            "jobs": {
                "test": {
                    "runs-on": "ubuntu-latest",
                    "steps": [
                        {"uses": "actions/checkout@v4"},
                        {
                            "name": "Set up Python",
                            "uses": "actions/setup-python@v5",
                            "with": {"python-version": "3.11"},
                        },
                        {
                            "name": "Install dependencies",
                            "run": "pip install -r requirements.txt",
                        },
                        {
                            "name": "Run tests",
                            "run": "pytest",
                        },
                    ],
                },
            },
        },
        "node": {
            "name": "Node.js CI",
            "on": {
                "push": {"branches": ["main", "master"]},
                "pull_request": {"branches": ["main", "master"]},
            },
            "jobs": {
                "test": {
                    "runs-on": "ubuntu-latest",
                    "steps": [
                        {"uses": "actions/checkout@v4"},
                        {
                            "name": "Set up Node.js",
                            "uses": "actions/setup-node@v4",
                            "with": {"node-version": "20"},
                        },
                        {
                            "name": "Install dependencies",
                            "run": "npm ci",
                        },
                        {
                            "name": "Run tests",
                            "run": "npm test",
                        },
                    ],
                },
            },
        },
        "go": {
            "name": "Go CI",
            "on": {
                "push": {"branches": ["main", "master"]},
                "pull_request": {"branches": ["main", "master"]},
            },
            "jobs": {
                "test": {
                    "runs-on": "ubuntu-latest",
                    "steps": [
                        {"uses": "actions/checkout@v4"},
                        {
                            "name": "Set up Go",
                            "uses": "actions/setup-go@v5",
                            "with": {"go-version": "1.21"},
                        },
                        {
                            "name": "Run tests",
                            "run": "go test -v ./...",
                        },
                    ],
                },
            },
        },
    }

    def __init__(self, workspace_path: Optional[str] = None):
        """Initialize workflow generator.

        Args:
            workspace_path: Path to project root
        """
        self.workspace_path = Path(workspace_path) if workspace_path else Path.cwd()
        self.workflows_dir = self.workspace_path / ".github" / "workflows"

    def detect_project_type(self) -> Optional[str]:
        """Detect project type from files."""
        if (self.workspace_path / "pyproject.toml").exists():
            return "python"
        if (self.workspace_path / "requirements.txt").exists():
            return "python"
        if (self.workspace_path / "package.json").exists():
            return "node"
        if (self.workspace_path / "go.mod").exists():
            return "go"
        if (self.workspace_path / "Cargo.toml").exists():
            return "rust"
        return None

    def generate_workflow(
        self,
        project_type: Optional[str] = None,
        name: str = "ci.yml",
        additional_steps: Optional[list[dict]] = None,
    ) -> str:
        """Generate a workflow file.

        Args:
            project_type: Project type (python, node, go, etc.)
            name: Workflow file name
            additional_steps: Additional steps to add

        Returns:
            YAML content
        """
        if not YAML_AVAILABLE:
            raise RuntimeError("PyYAML not installed. Run: pip install pyyaml")

        if project_type is None:
            project_type = self.detect_project_type()

        if project_type not in self.TEMPLATES:
            raise ValueError(f"No template for project type: {project_type}")

        workflow = self.TEMPLATES[project_type].copy()

        if additional_steps:
            workflow["jobs"]["test"]["steps"].extend(additional_steps)

        return yaml.dump(workflow, default_flow_style=False, sort_keys=False)

    def write_workflow(
        self,
        content: str,
        name: str = "ci.yml",
    ) -> Path:
        """Write workflow file to disk.

        Args:
            content: YAML content
            name: Workflow file name

        Returns:
            Path to created file
        """
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        workflow_path = self.workflows_dir / name
        workflow_path.write_text(content)
        return workflow_path

    def list_workflows(self) -> list[Path]:
        """List existing workflow files."""
        if not self.workflows_dir.exists():
            return []
        return list(self.workflows_dir.glob("*.yml")) + list(self.workflows_dir.glob("*.yaml"))


class TestResultParser:
    """Parse test results from CI logs."""

    # Patterns for different test frameworks
    PATTERNS = {
        "pytest": {
            "summary": r"=+\s*([\d]+)\s+passed.*?(?:(\d+)\s+failed)?.*?(?:(\d+)\s+skipped)?.*?(?:(\d+)\s+error)?.*?in\s+([\d.]+)s",
            "failed_test": r"FAILED\s+([^\s]+)",
        },
        "jest": {
            "summary": r"Tests:\s+(?:(\d+)\s+failed,\s+)?(?:(\d+)\s+skipped,\s+)?(\d+)\s+passed,\s+(\d+)\s+total",
            "failed_test": r"✕\s+(.+?)(?:\s+\(\d+\s*ms\))?$",
        },
        "go": {
            "summary": r"(ok|FAIL)\s+.+\s+([\d.]+)s",
            "failed_test": r"---\s+FAIL:\s+(\w+)",
        },
        "mocha": {
            "summary": r"(\d+)\s+passing.*?(?:(\d+)\s+failing)?",
            "failed_test": r"\d+\)\s+(.+):",
        },
    }

    def parse(self, log_content: str) -> TestResult:
        """Parse test results from log content.

        Args:
            log_content: CI log content

        Returns:
            TestResult
        """
        result = TestResult()

        # Try each framework pattern
        for framework, patterns in self.PATTERNS.items():
            summary_match = re.search(patterns["summary"], log_content, re.MULTILINE)
            if summary_match:
                result = self._parse_framework(framework, summary_match, log_content, patterns)
                break

        return result

    def _parse_framework(
        self,
        framework: str,
        summary_match: re.Match,
        log_content: str,
        patterns: dict,
    ) -> TestResult:
        """Parse results for specific framework."""
        result = TestResult()

        if framework == "pytest":
            groups = summary_match.groups()
            result.passed = int(groups[0]) if groups[0] else 0
            result.failed = int(groups[1]) if groups[1] else 0
            result.skipped = int(groups[2]) if groups[2] else 0
            result.errors = int(groups[3]) if groups[3] else 0
            result.duration_seconds = float(groups[4]) if groups[4] else 0
            result.total = result.passed + result.failed + result.skipped + result.errors

        elif framework == "jest":
            groups = summary_match.groups()
            result.failed = int(groups[0]) if groups[0] else 0
            result.skipped = int(groups[1]) if groups[1] else 0
            result.passed = int(groups[2]) if groups[2] else 0
            result.total = int(groups[3]) if groups[3] else 0

        elif framework == "go":
            # Count ok/FAIL lines
            ok_count = len(re.findall(r"^ok\s+", log_content, re.MULTILINE))
            fail_count = len(re.findall(r"^FAIL\s+", log_content, re.MULTILINE))
            result.passed = ok_count
            result.failed = fail_count
            result.total = ok_count + fail_count

        elif framework == "mocha":
            groups = summary_match.groups()
            result.passed = int(groups[0]) if groups[0] else 0
            result.failed = int(groups[1]) if groups[1] else 0
            result.total = result.passed + result.failed

        # Extract failed test names
        failed_pattern = patterns.get("failed_test")
        if failed_pattern:
            failed_matches = re.findall(failed_pattern, log_content, re.MULTILINE)
            result.failed_tests = failed_matches[:20]  # Limit

        return result


class CICDManager:
    """Manage CI/CD operations.

    Usage:
        manager = CICDManager(workspace_path="/path/to/project")

        # Get pipeline status
        runs = await manager.get_recent_runs()

        # Generate workflow
        manager.generate_workflow("python")

        # Parse test results
        results = await manager.get_test_results(run_id)
    """

    def __init__(
        self,
        workspace_path: Optional[str] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
    ):
        """Initialize CI/CD manager.

        Args:
            workspace_path: Path to project root
            owner: GitHub owner (auto-detected from git remote)
            repo: GitHub repo (auto-detected from git remote)
        """
        self.workspace_path = Path(workspace_path) if workspace_path else Path.cwd()
        self.owner = owner
        self.repo = repo

        # Auto-detect owner/repo from git remote
        if not self.owner or not self.repo:
            self._detect_repo()

        self.github = GitHubActionsClient(self.owner or "", self.repo or "")
        self.generator = WorkflowGenerator(str(self.workspace_path))
        self.parser = TestResultParser()

    def _detect_repo(self) -> None:
        """Detect owner/repo from git remote."""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                cwd=str(self.workspace_path),
            )
            if result.returncode == 0:
                url = result.stdout.strip()
                # Parse GitHub URL
                match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", url)
                if match:
                    self.owner = match.group(1)
                    self.repo = match.group(2)
        except Exception:
            pass

    async def get_recent_runs(
        self,
        branch: Optional[str] = None,
        limit: int = 10,
    ) -> list[WorkflowRun]:
        """Get recent workflow runs.

        Args:
            branch: Filter by branch
            limit: Maximum runs

        Returns:
            List of runs
        """
        return await self.github.list_workflow_runs(branch=branch, limit=limit)

    async def get_run_status(self, run_id: int) -> Optional[dict[str, Any]]:
        """Get detailed status of a run.

        Args:
            run_id: Workflow run ID

        Returns:
            Status dict with run and jobs info
        """
        run = await self.github.get_workflow_run(run_id)
        if not run:
            return None

        jobs = await self.github.get_run_jobs(run_id)

        return {
            "run": run.to_dict(),
            "jobs": [job.to_dict() for job in jobs],
        }

    async def get_test_results(self, run_id: int) -> Optional[TestResult]:
        """Get parsed test results from a run.

        Args:
            run_id: Workflow run ID

        Returns:
            TestResult or None
        """
        logs = await self.github.get_run_logs(run_id)
        if not logs:
            return None

        return self.parser.parse(logs)

    def generate_workflow(
        self,
        project_type: Optional[str] = None,
        name: str = "ci.yml",
    ) -> Path:
        """Generate and write a CI workflow.

        Args:
            project_type: Project type
            name: Workflow file name

        Returns:
            Path to created workflow file
        """
        content = self.generator.generate_workflow(project_type, name)
        return self.generator.write_workflow(content, name)

    async def trigger_workflow(
        self,
        workflow_id: str = "ci.yml",
        ref: str = "main",
    ) -> bool:
        """Trigger a workflow run.

        Args:
            workflow_id: Workflow file name
            ref: Git ref

        Returns:
            True if triggered
        """
        return await self.github.trigger_workflow(workflow_id, ref)


# Tool handlers for integration with ToolBroker

async def ci_status_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler for getting CI status.

    Args:
        workspace: Optional workspace path
        branch: Optional branch filter
        run_id: Optional specific run ID
        limit: Maximum runs to return (default: 5)

    Returns:
        CI status information
    """
    workspace = args.get("workspace")
    branch = args.get("branch")
    run_id = args.get("run_id")
    limit = args.get("limit", 5)

    manager = CICDManager(workspace_path=workspace)

    if run_id:
        status = await manager.get_run_status(int(run_id))
        if status:
            return {"status": "success", **status}
        return {"status": "error", "error": "Run not found"}

    runs = await manager.get_recent_runs(branch=branch, limit=limit)

    return {
        "status": "success",
        "runs": [run.to_dict() for run in runs],
        "repo": f"{manager.owner}/{manager.repo}",
    }


async def ci_test_results_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler for getting test results from CI.

    Args:
        workspace: Optional workspace path
        run_id: Workflow run ID

    Returns:
        Parsed test results
    """
    workspace = args.get("workspace")
    run_id = args.get("run_id")

    if not run_id:
        return {"status": "error", "error": "run_id is required"}

    manager = CICDManager(workspace_path=workspace)
    results = await manager.get_test_results(int(run_id))

    if results:
        return {"status": "success", "results": results.to_dict()}
    return {"status": "error", "error": "Could not parse test results"}


async def ci_generate_workflow_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler for generating CI workflow.

    Args:
        workspace: Optional workspace path
        project_type: Project type (python, node, go)
        name: Workflow file name (default: ci.yml)

    Returns:
        Created workflow info
    """
    workspace = args.get("workspace")
    project_type = args.get("project_type")
    name = args.get("name", "ci.yml")

    manager = CICDManager(workspace_path=workspace)

    try:
        path = manager.generate_workflow(project_type, name)
        content = path.read_text()
        return {
            "status": "success",
            "path": str(path),
            "content": content,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def ci_trigger_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler for triggering CI workflow.

    Args:
        workspace: Optional workspace path
        workflow: Workflow file name (default: ci.yml)
        ref: Git ref (default: main)

    Returns:
        Trigger status
    """
    workspace = args.get("workspace")
    workflow = args.get("workflow", "ci.yml")
    ref = args.get("ref", "main")

    manager = CICDManager(workspace_path=workspace)
    success = await manager.trigger_workflow(workflow, ref)

    if success:
        return {"status": "success", "message": f"Triggered {workflow} on {ref}"}
    return {"status": "error", "error": "Failed to trigger workflow"}


__all__ = [
    "CICDManager",
    "GitHubActionsClient",
    "WorkflowGenerator",
    "TestResultParser",
    "WorkflowRun",
    "WorkflowJob",
    "TestResult",
    "ci_status_handler",
    "ci_test_results_handler",
    "ci_generate_workflow_handler",
    "ci_trigger_handler",
]
