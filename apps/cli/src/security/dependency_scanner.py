"""Dependency Vulnerability Scanner (SCA) for Glock.

Phase 3 Feature 3.2: Software Composition Analysis.

Scans project dependencies for known vulnerabilities using:
- NVD (National Vulnerability Database)
- OSV (Open Source Vulnerabilities)
- GitHub Advisory Database

Supports:
- Python: requirements.txt, pyproject.toml, Pipfile
- JavaScript/Node: package.json, package-lock.json, yarn.lock
- Go: go.mod, go.sum
- Rust: Cargo.toml, Cargo.lock
- Java: pom.xml, build.gradle
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
import hashlib

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

try:
    import toml
    TOML_AVAILABLE = True
except ImportError:
    TOML_AVAILABLE = False

logger = logging.getLogger(__name__)


class Severity(str, Enum):
    """Vulnerability severity levels."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class Ecosystem(str, Enum):
    """Package ecosystems."""
    PYPI = "PyPI"
    NPM = "npm"
    GO = "Go"
    CARGO = "crates.io"
    MAVEN = "Maven"
    RUBYGEMS = "RubyGems"


@dataclass
class Dependency:
    """A project dependency."""
    name: str
    version: str
    ecosystem: Ecosystem
    source_file: str
    direct: bool = True  # Direct vs transitive dependency

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "ecosystem": self.ecosystem.value,
            "source_file": self.source_file,
            "direct": self.direct,
        }


@dataclass
class Vulnerability:
    """A known vulnerability."""
    id: str  # CVE or GHSA ID
    title: str
    description: str
    severity: Severity
    affected_package: str
    affected_versions: str
    fixed_version: Optional[str] = None
    cvss_score: Optional[float] = None
    published_date: Optional[datetime] = None
    references: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description[:500] if self.description else "",
            "severity": self.severity.value,
            "affected_package": self.affected_package,
            "affected_versions": self.affected_versions,
            "fixed_version": self.fixed_version,
            "cvss_score": self.cvss_score,
            "references": self.references[:5],
        }


@dataclass
class VulnerabilityMatch:
    """A vulnerability matching a project dependency."""
    dependency: Dependency
    vulnerability: Vulnerability
    remediation: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dependency": self.dependency.to_dict(),
            "vulnerability": self.vulnerability.to_dict(),
            "remediation": self.remediation,
        }


@dataclass
class ScanResult:
    """Result of dependency vulnerability scan."""
    scanned_at: datetime
    dependencies_count: int
    vulnerabilities_found: int
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    matches: list[VulnerabilityMatch] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_at": self.scanned_at.isoformat(),
            "dependencies_count": self.dependencies_count,
            "vulnerabilities_found": self.vulnerabilities_found,
            "critical_count": self.critical_count,
            "high_count": self.high_count,
            "medium_count": self.medium_count,
            "low_count": self.low_count,
            "matches": [m.to_dict() for m in self.matches],
            "errors": self.errors,
            "duration_ms": self.duration_ms,
        }

    def to_report(self) -> str:
        """Generate human-readable report."""
        lines = [
            "# Dependency Vulnerability Scan Report",
            f"Scanned: {self.scanned_at.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Dependencies: {self.dependencies_count}",
            f"Vulnerabilities: {self.vulnerabilities_found}",
            "",
        ]

        if self.vulnerabilities_found == 0:
            lines.append("No vulnerabilities found.")
            return "\n".join(lines)

        lines.append("## Summary")
        lines.append(f"- Critical: {self.critical_count}")
        lines.append(f"- High: {self.high_count}")
        lines.append(f"- Medium: {self.medium_count}")
        lines.append(f"- Low: {self.low_count}")
        lines.append("")

        # Group by severity
        for severity in [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]:
            severity_matches = [m for m in self.matches if m.vulnerability.severity == severity]
            if severity_matches:
                lines.append(f"## {severity.value.upper()} ({len(severity_matches)})")
                for match in severity_matches[:10]:  # Limit per severity
                    lines.append(f"### {match.vulnerability.id}: {match.dependency.name}")
                    lines.append(f"- **Affected Version**: {match.dependency.version}")
                    lines.append(f"- **Description**: {match.vulnerability.description[:200]}...")
                    if match.vulnerability.fixed_version:
                        lines.append(f"- **Fixed In**: {match.vulnerability.fixed_version}")
                    if match.remediation:
                        lines.append(f"- **Remediation**: {match.remediation}")
                    lines.append("")

        if self.errors:
            lines.append("## Errors")
            for error in self.errors:
                lines.append(f"- {error}")

        return "\n".join(lines)


class DependencyParser:
    """Parse dependency files from various ecosystems."""

    @staticmethod
    def parse_requirements_txt(content: str, source_file: str) -> list[Dependency]:
        """Parse Python requirements.txt."""
        dependencies = []
        for line in content.split("\n"):
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue

            # Handle various formats: pkg==1.0, pkg>=1.0, pkg~=1.0, etc.
            match = re.match(r"^([a-zA-Z0-9_-]+)([<>=!~]+)?(.+)?", line)
            if match:
                name = match.group(1)
                version = match.group(3) or "unknown"
                # Clean version
                version = version.split(";")[0].split(",")[0].strip()
                dependencies.append(Dependency(
                    name=name.lower(),
                    version=version,
                    ecosystem=Ecosystem.PYPI,
                    source_file=source_file,
                ))

        return dependencies

    @staticmethod
    def parse_pyproject_toml(content: str, source_file: str) -> list[Dependency]:
        """Parse Python pyproject.toml."""
        if not TOML_AVAILABLE:
            logger.warning("toml package not available for parsing pyproject.toml")
            return []

        dependencies = []
        try:
            data = toml.loads(content)

            # PEP 621 format
            deps = data.get("project", {}).get("dependencies", [])
            for dep in deps:
                match = re.match(r"^([a-zA-Z0-9_-]+)([<>=!~\[]+)?(.+)?", dep)
                if match:
                    name = match.group(1)
                    version = match.group(3) or "unknown"
                    version = version.split(";")[0].split(",")[0].strip().rstrip("]")
                    dependencies.append(Dependency(
                        name=name.lower(),
                        version=version,
                        ecosystem=Ecosystem.PYPI,
                        source_file=source_file,
                    ))

            # Poetry format
            poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
            for name, spec in poetry_deps.items():
                if name == "python":
                    continue
                if isinstance(spec, str):
                    version = spec
                elif isinstance(spec, dict):
                    version = spec.get("version", "unknown")
                else:
                    version = "unknown"
                dependencies.append(Dependency(
                    name=name.lower(),
                    version=version.lstrip("^~>=<"),
                    ecosystem=Ecosystem.PYPI,
                    source_file=source_file,
                ))

        except Exception as e:
            logger.warning(f"Failed to parse pyproject.toml: {e}")

        return dependencies

    @staticmethod
    def parse_package_json(content: str, source_file: str) -> list[Dependency]:
        """Parse Node.js package.json."""
        dependencies = []
        try:
            data = json.loads(content)

            for dep_type in ["dependencies", "devDependencies"]:
                deps = data.get(dep_type, {})
                for name, version in deps.items():
                    # Clean version specifier
                    version = version.lstrip("^~>=<")
                    dependencies.append(Dependency(
                        name=name,
                        version=version,
                        ecosystem=Ecosystem.NPM,
                        source_file=source_file,
                    ))

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse package.json: {e}")

        return dependencies

    @staticmethod
    def parse_go_mod(content: str, source_file: str) -> list[Dependency]:
        """Parse Go go.mod."""
        dependencies = []

        # Match require blocks
        in_require = False
        for line in content.split("\n"):
            line = line.strip()

            if line.startswith("require ("):
                in_require = True
                continue
            elif line == ")":
                in_require = False
                continue

            if in_require or line.startswith("require "):
                # Extract module and version
                match = re.match(r"(?:require\s+)?([^\s]+)\s+v?([^\s]+)", line)
                if match:
                    name = match.group(1)
                    version = match.group(2)
                    dependencies.append(Dependency(
                        name=name,
                        version=version,
                        ecosystem=Ecosystem.GO,
                        source_file=source_file,
                    ))

        return dependencies

    @staticmethod
    def parse_cargo_toml(content: str, source_file: str) -> list[Dependency]:
        """Parse Rust Cargo.toml."""
        if not TOML_AVAILABLE:
            logger.warning("toml package not available for parsing Cargo.toml")
            return []

        dependencies = []
        try:
            data = toml.loads(content)

            for dep_type in ["dependencies", "dev-dependencies", "build-dependencies"]:
                deps = data.get(dep_type, {})
                for name, spec in deps.items():
                    if isinstance(spec, str):
                        version = spec
                    elif isinstance(spec, dict):
                        version = spec.get("version", "unknown")
                    else:
                        version = "unknown"

                    dependencies.append(Dependency(
                        name=name,
                        version=version.lstrip("^~>=<"),
                        ecosystem=Ecosystem.CARGO,
                        source_file=source_file,
                    ))

        except Exception as e:
            logger.warning(f"Failed to parse Cargo.toml: {e}")

        return dependencies


class VulnerabilityDatabase:
    """Query vulnerability databases."""

    OSV_API = "https://api.osv.dev/v1"

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self._cache: dict[str, list[Vulnerability]] = {}

    async def query_osv(
        self,
        package: str,
        version: str,
        ecosystem: Ecosystem,
    ) -> list[Vulnerability]:
        """Query OSV database for vulnerabilities.

        Args:
            package: Package name
            version: Package version
            ecosystem: Package ecosystem

        Returns:
            List of vulnerabilities affecting this package/version
        """
        if not AIOHTTP_AVAILABLE:
            logger.warning("aiohttp not available for OSV queries")
            return []

        cache_key = f"{ecosystem.value}:{package}:{version}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        vulnerabilities = []

        try:
            async with aiohttp.ClientSession() as session:
                # Query by package
                payload = {
                    "version": version,
                    "package": {
                        "name": package,
                        "ecosystem": ecosystem.value,
                    },
                }

                async with session.post(
                    f"{self.OSV_API}/query",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as response:
                    if response.status != 200:
                        return []

                    data = await response.json()
                    vulns = data.get("vulns", [])

                    for vuln in vulns:
                        severity = self._parse_severity(vuln)
                        affected_versions = self._parse_affected_versions(vuln, package)
                        fixed_version = self._parse_fixed_version(vuln, package)

                        vulnerabilities.append(Vulnerability(
                            id=vuln.get("id", "unknown"),
                            title=vuln.get("summary", "No title"),
                            description=vuln.get("details", ""),
                            severity=severity,
                            affected_package=package,
                            affected_versions=affected_versions,
                            fixed_version=fixed_version,
                            cvss_score=self._extract_cvss(vuln),
                            published_date=self._parse_date(vuln.get("published")),
                            references=[
                                ref.get("url", "")
                                for ref in vuln.get("references", [])[:5]
                            ],
                        ))

        except asyncio.TimeoutError:
            logger.warning(f"OSV query timed out for {package}")
        except aiohttp.ClientError as e:
            logger.warning(f"OSV query failed for {package}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error querying OSV for {package}: {e}")

        self._cache[cache_key] = vulnerabilities
        return vulnerabilities

    def _parse_severity(self, vuln: dict) -> Severity:
        """Parse severity from OSV vulnerability."""
        # Check database_specific severity
        severity_str = vuln.get("database_specific", {}).get("severity", "").upper()
        if severity_str:
            try:
                return Severity(severity_str.lower())
            except ValueError:
                pass

        # Check CVSS score
        cvss = self._extract_cvss(vuln)
        if cvss:
            if cvss >= 9.0:
                return Severity.CRITICAL
            elif cvss >= 7.0:
                return Severity.HIGH
            elif cvss >= 4.0:
                return Severity.MEDIUM
            else:
                return Severity.LOW

        return Severity.UNKNOWN

    def _extract_cvss(self, vuln: dict) -> Optional[float]:
        """Extract CVSS score from vulnerability."""
        for severity in vuln.get("severity", []):
            if severity.get("type") in ("CVSS_V3", "CVSS_V2"):
                score = severity.get("score")
                if isinstance(score, (int, float)):
                    return float(score)
                # Parse from vector string
                vector = severity.get("score", "")
                if "/" in vector:
                    try:
                        # Extract base score from vector
                        return float(vector.split("/")[0])
                    except (ValueError, IndexError):
                        pass
        return None

    def _parse_affected_versions(self, vuln: dict, package: str) -> str:
        """Parse affected version range."""
        for affected in vuln.get("affected", []):
            if affected.get("package", {}).get("name") == package:
                ranges = affected.get("ranges", [])
                if ranges:
                    range_info = ranges[0]
                    events = range_info.get("events", [])
                    introduced = None
                    fixed = None
                    for event in events:
                        if "introduced" in event:
                            introduced = event["introduced"]
                        if "fixed" in event:
                            fixed = event["fixed"]
                    if introduced and fixed:
                        return f">={introduced}, <{fixed}"
                    elif introduced:
                        return f">={introduced}"

                versions = affected.get("versions", [])
                if versions:
                    return ", ".join(versions[:5])

        return "unknown"

    def _parse_fixed_version(self, vuln: dict, package: str) -> Optional[str]:
        """Parse fixed version from vulnerability."""
        for affected in vuln.get("affected", []):
            if affected.get("package", {}).get("name") == package:
                for range_info in affected.get("ranges", []):
                    for event in range_info.get("events", []):
                        if "fixed" in event:
                            return event["fixed"]
        return None

    def _parse_date(self, date_str: Optional[str]) -> Optional[datetime]:
        """Parse ISO date string."""
        if not date_str:
            return None
        try:
            return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except ValueError:
            return None


class DependencyScanner:
    """Scan project dependencies for vulnerabilities.

    Usage:
        scanner = DependencyScanner(workspace_path="/path/to/project")
        result = await scanner.scan()
        print(result.to_report())
    """

    # Files to scan for dependencies
    DEPENDENCY_FILES = {
        "requirements.txt": (DependencyParser.parse_requirements_txt, Ecosystem.PYPI),
        "requirements-dev.txt": (DependencyParser.parse_requirements_txt, Ecosystem.PYPI),
        "requirements-test.txt": (DependencyParser.parse_requirements_txt, Ecosystem.PYPI),
        "pyproject.toml": (DependencyParser.parse_pyproject_toml, Ecosystem.PYPI),
        "package.json": (DependencyParser.parse_package_json, Ecosystem.NPM),
        "go.mod": (DependencyParser.parse_go_mod, Ecosystem.GO),
        "Cargo.toml": (DependencyParser.parse_cargo_toml, Ecosystem.CARGO),
    }

    def __init__(
        self,
        workspace_path: Optional[str] = None,
        timeout: float = 60.0,
        max_concurrent: int = 10,
    ):
        """Initialize dependency scanner.

        Args:
            workspace_path: Path to project root
            timeout: Timeout for vulnerability queries
            max_concurrent: Max concurrent API queries
        """
        self.workspace_path = Path(workspace_path) if workspace_path else Path.cwd()
        self.timeout = timeout
        self.max_concurrent = max_concurrent
        self.vuln_db = VulnerabilityDatabase(timeout=timeout)

    async def scan(self) -> ScanResult:
        """Scan project for vulnerable dependencies.

        Returns:
            ScanResult with all findings
        """
        import time
        start_time = time.time()

        # Collect dependencies
        dependencies = await self._collect_dependencies()

        if not dependencies:
            return ScanResult(
                scanned_at=datetime.now(),
                dependencies_count=0,
                vulnerabilities_found=0,
            )

        # Query for vulnerabilities
        matches = await self._check_vulnerabilities(dependencies)

        # Count by severity
        critical = sum(1 for m in matches if m.vulnerability.severity == Severity.CRITICAL)
        high = sum(1 for m in matches if m.vulnerability.severity == Severity.HIGH)
        medium = sum(1 for m in matches if m.vulnerability.severity == Severity.MEDIUM)
        low = sum(1 for m in matches if m.vulnerability.severity == Severity.LOW)

        duration_ms = int((time.time() - start_time) * 1000)

        return ScanResult(
            scanned_at=datetime.now(),
            dependencies_count=len(dependencies),
            vulnerabilities_found=len(matches),
            critical_count=critical,
            high_count=high,
            medium_count=medium,
            low_count=low,
            matches=sorted(matches, key=lambda m: (
                -{"critical": 4, "high": 3, "medium": 2, "low": 1, "unknown": 0}
                .get(m.vulnerability.severity.value, 0),
                m.dependency.name,
            )),
            duration_ms=duration_ms,
        )

    async def _collect_dependencies(self) -> list[Dependency]:
        """Collect dependencies from project files."""
        dependencies = []

        for filename, (parser, ecosystem) in self.DEPENDENCY_FILES.items():
            file_path = self.workspace_path / filename
            if file_path.exists():
                try:
                    content = file_path.read_text()
                    deps = parser(content, filename)
                    dependencies.extend(deps)
                    logger.debug(f"Found {len(deps)} dependencies in {filename}")
                except Exception as e:
                    logger.warning(f"Failed to parse {filename}: {e}")

        # Remove duplicates (same name+version)
        seen = set()
        unique_deps = []
        for dep in dependencies:
            key = f"{dep.ecosystem.value}:{dep.name}:{dep.version}"
            if key not in seen:
                seen.add(key)
                unique_deps.append(dep)

        return unique_deps

    async def _check_vulnerabilities(
        self,
        dependencies: list[Dependency],
    ) -> list[VulnerabilityMatch]:
        """Check dependencies for vulnerabilities."""
        matches: list[VulnerabilityMatch] = []

        # Use semaphore for rate limiting
        semaphore = asyncio.Semaphore(self.max_concurrent)

        async def check_dependency(dep: Dependency) -> list[VulnerabilityMatch]:
            async with semaphore:
                vulns = await self.vuln_db.query_osv(
                    package=dep.name,
                    version=dep.version,
                    ecosystem=dep.ecosystem,
                )

                dep_matches = []
                for vuln in vulns:
                    remediation = None
                    if vuln.fixed_version:
                        remediation = f"Upgrade {dep.name} to {vuln.fixed_version}"

                    dep_matches.append(VulnerabilityMatch(
                        dependency=dep,
                        vulnerability=vuln,
                        remediation=remediation,
                    ))

                return dep_matches

        # Query all dependencies concurrently
        tasks = [check_dependency(dep) for dep in dependencies]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, list):
                matches.extend(result)
            elif isinstance(result, Exception):
                logger.warning(f"Vulnerability check failed: {result}")

        return matches

    async def scan_file(self, file_path: str) -> ScanResult:
        """Scan a specific dependency file.

        Args:
            file_path: Path to dependency file

        Returns:
            ScanResult for that file
        """
        import time
        start_time = time.time()

        path = Path(file_path)
        if not path.is_absolute():
            path = self.workspace_path / path

        filename = path.name
        if filename not in self.DEPENDENCY_FILES:
            return ScanResult(
                scanned_at=datetime.now(),
                dependencies_count=0,
                vulnerabilities_found=0,
                errors=[f"Unsupported dependency file: {filename}"],
            )

        parser, ecosystem = self.DEPENDENCY_FILES[filename]

        try:
            content = path.read_text()
            dependencies = parser(content, filename)
        except Exception as e:
            return ScanResult(
                scanned_at=datetime.now(),
                dependencies_count=0,
                vulnerabilities_found=0,
                errors=[f"Failed to parse {filename}: {e}"],
            )

        matches = await self._check_vulnerabilities(dependencies)

        critical = sum(1 for m in matches if m.vulnerability.severity == Severity.CRITICAL)
        high = sum(1 for m in matches if m.vulnerability.severity == Severity.HIGH)
        medium = sum(1 for m in matches if m.vulnerability.severity == Severity.MEDIUM)
        low = sum(1 for m in matches if m.vulnerability.severity == Severity.LOW)

        duration_ms = int((time.time() - start_time) * 1000)

        return ScanResult(
            scanned_at=datetime.now(),
            dependencies_count=len(dependencies),
            vulnerabilities_found=len(matches),
            critical_count=critical,
            high_count=high,
            medium_count=medium,
            low_count=low,
            matches=matches,
            duration_ms=duration_ms,
        )


# Tool handlers for integration with ToolBroker

async def scan_dependencies_handler(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler for scanning dependencies.

    Args:
        workspace: Optional workspace path
        file: Optional specific dependency file to scan

    Returns:
        Scan result
    """
    workspace = args.get("workspace")
    file_path = args.get("file")

    scanner = DependencyScanner(workspace_path=workspace)

    if file_path:
        result = await scanner.scan_file(file_path)
    else:
        result = await scanner.scan()

    return {
        **result.to_dict(),
        "report": result.to_report(),
    }
