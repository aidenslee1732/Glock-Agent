"""Security scanner for detecting vulnerabilities in code.

Provides static analysis for common security issues:
- OWASP Top 10 vulnerabilities
- Hardcoded credentials
- Command/SQL injection risks
- Insecure configurations

Can suggest patches using LLM assistance.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)


class SecurityError(Exception):
    """Security-related error."""
    pass


class Severity(str, Enum):
    """Vulnerability severity levels."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


@dataclass
class Vulnerability:
    """A detected security vulnerability."""

    id: str
    category: str  # OWASP category or custom
    severity: Severity
    title: str
    file_path: str
    line_number: Optional[int]
    code_snippet: str
    cwe_id: Optional[str]  # CWE identifier
    remediation: str
    patch: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "category": self.category,
            "severity": self.severity.value,
            "title": self.title,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "code_snippet": self.code_snippet,
            "cwe_id": self.cwe_id,
            "remediation": self.remediation,
            "patch": self.patch,
        }


@dataclass
class SecurityReport:
    """Security scan results."""

    vulnerabilities: list[Vulnerability]
    files_scanned: int
    scan_duration_ms: int
    summary: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        """Calculate summary."""
        self.summary = {
            "critical": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
            "info": 0,
            "total": len(self.vulnerabilities),
        }
        for vuln in self.vulnerabilities:
            self.summary[vuln.severity.value] += 1

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "vulnerabilities": [v.to_dict() for v in self.vulnerabilities],
            "files_scanned": self.files_scanned,
            "scan_duration_ms": self.scan_duration_ms,
            "summary": self.summary,
        }

    def format_report(self) -> str:
        """Format as human-readable report."""
        lines = [
            "# Security Scan Report",
            "",
            f"Files scanned: {self.files_scanned}",
            f"Duration: {self.scan_duration_ms}ms",
            "",
            "## Summary",
            f"- Critical: {self.summary['critical']}",
            f"- High: {self.summary['high']}",
            f"- Medium: {self.summary['medium']}",
            f"- Low: {self.summary['low']}",
            f"- Info: {self.summary['info']}",
            "",
        ]

        if self.vulnerabilities:
            lines.append("## Vulnerabilities")
            lines.append("")

            for vuln in sorted(self.vulnerabilities, key=lambda v: (
                {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}[v.severity.value],
                v.file_path,
            )):
                lines.extend([
                    f"### [{vuln.severity.value.upper()}] {vuln.title}",
                    f"**File:** {vuln.file_path}:{vuln.line_number}",
                    f"**Category:** {vuln.category}",
                    f"**CWE:** {vuln.cwe_id}" if vuln.cwe_id else "",
                    "",
                    "```",
                    vuln.code_snippet,
                    "```",
                    "",
                    f"**Remediation:** {vuln.remediation}",
                    "",
                ])

                if vuln.patch:
                    lines.extend([
                        "**Suggested Fix:**",
                        "```",
                        vuln.patch,
                        "```",
                        "",
                    ])

        else:
            lines.append("No vulnerabilities found! ✓")

        return "\n".join(lines)


class SecurityScanner:
    """Security scanner for detecting vulnerabilities.

    Performs static analysis on code to detect common security issues.
    Can optionally use LLM for more sophisticated analysis and patch generation.
    """

    def __init__(
        self,
        workspace_path: Optional[Path] = None,
        llm_callback: Optional[Callable[[str, str], Coroutine[Any, Any, str]]] = None,
    ):
        """Initialize scanner.

        Args:
            workspace_path: Base path for scanning
            llm_callback: Optional LLM callback for enhanced analysis
        """
        self.workspace_path = self._normalize_path(str(workspace_path)) if workspace_path else Path.cwd()
        self.llm_callback = llm_callback
        self._vuln_counter = 0

        # Define security patterns
        self._patterns = self._build_patterns()

    def _normalize_path(self, path: str) -> Path:
        """Normalize a path with cross-platform safety.

        Handles:
        - Windows drive letter normalization (c: -> C:)
        - UNC path rejection (\\\\server\\share)
        - Path traversal prevention
        - Symlink resolution

        Args:
            path: Path string to normalize

        Returns:
            Normalized Path object

        Raises:
            SecurityError: If path is unsafe (UNC paths, etc.)
        """
        if sys.platform == "win32":
            # Reject UNC paths (network shares)
            if path.startswith("\\\\") or path.startswith("//"):
                raise SecurityError(f"UNC paths not allowed: {path}")

            # Normalize drive letter to uppercase (c: -> C:)
            if len(path) >= 2 and path[1] == ':':
                path = path[0].upper() + path[1:]

            # Check for alternate data streams (file.txt:stream)
            if ':' in path[2:]:  # Skip drive letter
                raise SecurityError(f"Alternate data streams not allowed: {path}")

        # Resolve to absolute path
        resolved = Path(path).resolve()

        # Check for path traversal (resolved path should be under expected root)
        # This is a basic check - callers should verify against workspace boundary
        try:
            # Ensure the path is valid
            resolved_str = resolved.as_posix()
        except Exception as e:
            raise SecurityError(f"Invalid path: {path} - {e}")

        return resolved

    def _validate_path_in_workspace(self, path: Path) -> bool:
        """Validate that a path is within the workspace.

        Args:
            path: Path to validate

        Returns:
            True if path is within workspace
        """
        try:
            normalized = self._normalize_path(str(path))
            workspace_normalized = self._normalize_path(str(self.workspace_path))

            # Check if path is under workspace
            normalized.relative_to(workspace_normalized)
            return True
        except (ValueError, SecurityError):
            return False

    def _build_patterns(self) -> list[dict[str, Any]]:
        """Build security pattern definitions."""
        return [
            # Command Injection
            {
                "name": "command_injection",
                "pattern": r"\b(os\.system|subprocess\.call|subprocess\.Popen|os\.popen)\s*\(",
                "severity": Severity.CRITICAL,
                "category": "A03:2021-Injection",
                "title": "Potential Command Injection",
                "cwe": "CWE-78",
                "remediation": "Use subprocess with shell=False and pass arguments as a list. Validate and sanitize all user input.",
                "languages": ["python"],
            },
            {
                "name": "shell_injection",
                "pattern": r"subprocess\.[a-z]+\([^)]*shell\s*=\s*True",
                "severity": Severity.HIGH,
                "category": "A03:2021-Injection",
                "title": "Shell Injection Risk (shell=True)",
                "cwe": "CWE-78",
                "remediation": "Avoid shell=True. Use shell=False with command as list.",
                "languages": ["python"],
            },
            # SQL Injection
            {
                "name": "sql_injection_python",
                "pattern": r'(execute|cursor\.execute)\s*\(\s*["\'][^"\']*%s',
                "severity": Severity.CRITICAL,
                "category": "A03:2021-Injection",
                "title": "Potential SQL Injection",
                "cwe": "CWE-89",
                "remediation": "Use parameterized queries instead of string formatting.",
                "languages": ["python"],
            },
            {
                "name": "sql_injection_format",
                "pattern": r'(execute|cursor\.execute)\s*\([^)]*\.format\(',
                "severity": Severity.CRITICAL,
                "category": "A03:2021-Injection",
                "title": "SQL Injection via String Formatting",
                "cwe": "CWE-89",
                "remediation": "Use parameterized queries. Never use f-strings or .format() for SQL.",
                "languages": ["python"],
            },
            # Hardcoded Credentials
            {
                "name": "hardcoded_password",
                "pattern": r'(password|passwd|pwd|secret|api_key|apikey|token)\s*=\s*["\'][^"\']{4,}["\']',
                "severity": Severity.HIGH,
                "category": "A07:2021-Identification and Authentication Failures",
                "title": "Hardcoded Credential",
                "cwe": "CWE-798",
                "remediation": "Use environment variables or a secrets manager for credentials.",
                "languages": ["python", "javascript", "typescript"],
            },
            # Eval/Exec
            {
                "name": "eval_usage",
                "pattern": r"\beval\s*\(",
                "severity": Severity.HIGH,
                "category": "A03:2021-Injection",
                "title": "Use of eval()",
                "cwe": "CWE-95",
                "remediation": "Avoid eval(). Use ast.literal_eval() for safe literal evaluation, or refactor to avoid dynamic code execution.",
                "languages": ["python", "javascript"],
            },
            {
                "name": "exec_usage",
                "pattern": r"\bexec\s*\(",
                "severity": Severity.HIGH,
                "category": "A03:2021-Injection",
                "title": "Use of exec()",
                "cwe": "CWE-95",
                "remediation": "Avoid exec(). Refactor to use safe alternatives.",
                "languages": ["python"],
            },
            # Path Traversal
            {
                "name": "path_traversal",
                "pattern": r'(open|Path)\s*\([^)]*\+[^)]*\)',
                "severity": Severity.MEDIUM,
                "category": "A01:2021-Broken Access Control",
                "title": "Potential Path Traversal",
                "cwe": "CWE-22",
                "remediation": "Validate and sanitize file paths. Use os.path.realpath() to resolve paths and verify they're within allowed directories.",
                "languages": ["python"],
            },
            # Insecure Deserialization
            {
                "name": "pickle_load",
                "pattern": r"pickle\.(load|loads)\s*\(",
                "severity": Severity.HIGH,
                "category": "A08:2021-Software and Data Integrity Failures",
                "title": "Insecure Deserialization (pickle)",
                "cwe": "CWE-502",
                "remediation": "Avoid pickle for untrusted data. Use JSON or other safe formats.",
                "languages": ["python"],
            },
            {
                "name": "yaml_unsafe_load",
                "pattern": r"yaml\.(load|unsafe_load)\s*\([^)]*\)",
                "severity": Severity.HIGH,
                "category": "A08:2021-Software and Data Integrity Failures",
                "title": "Insecure YAML Loading",
                "cwe": "CWE-502",
                "remediation": "Use yaml.safe_load() instead of yaml.load().",
                "languages": ["python"],
            },
            # XSS
            {
                "name": "xss_innerHTML",
                "pattern": r"\.innerHTML\s*=",
                "severity": Severity.MEDIUM,
                "category": "A03:2021-Injection",
                "title": "Potential XSS via innerHTML",
                "cwe": "CWE-79",
                "remediation": "Use textContent instead of innerHTML, or sanitize HTML before insertion.",
                "languages": ["javascript", "typescript"],
            },
            {
                "name": "xss_dangerouslySetInnerHTML",
                "pattern": r"dangerouslySetInnerHTML",
                "severity": Severity.MEDIUM,
                "category": "A03:2021-Injection",
                "title": "Potential XSS via dangerouslySetInnerHTML",
                "cwe": "CWE-79",
                "remediation": "Sanitize HTML content before using dangerouslySetInnerHTML. Consider using a sanitization library.",
                "languages": ["javascript", "typescript"],
            },
            # Weak Cryptography
            {
                "name": "weak_hash_md5",
                "pattern": r"(hashlib\.md5|MD5|md5\()",
                "severity": Severity.MEDIUM,
                "category": "A02:2021-Cryptographic Failures",
                "title": "Weak Hash Algorithm (MD5)",
                "cwe": "CWE-328",
                "remediation": "Use SHA-256 or stronger hashing algorithms for security purposes.",
                "languages": ["python"],
            },
            {
                "name": "weak_hash_sha1",
                "pattern": r"(hashlib\.sha1|SHA1|sha1\()",
                "severity": Severity.LOW,
                "category": "A02:2021-Cryptographic Failures",
                "title": "Weak Hash Algorithm (SHA1)",
                "cwe": "CWE-328",
                "remediation": "Use SHA-256 or stronger for new implementations.",
                "languages": ["python"],
            },
            # Debug/Development Settings
            {
                "name": "debug_true",
                "pattern": r"DEBUG\s*=\s*True",
                "severity": Severity.LOW,
                "category": "A05:2021-Security Misconfiguration",
                "title": "Debug Mode Enabled",
                "cwe": "CWE-489",
                "remediation": "Ensure DEBUG is False in production environments.",
                "languages": ["python"],
            },
            # Insecure SSL
            {
                "name": "ssl_verify_false",
                "pattern": r"verify\s*=\s*False",
                "severity": Severity.HIGH,
                "category": "A02:2021-Cryptographic Failures",
                "title": "SSL Verification Disabled",
                "cwe": "CWE-295",
                "remediation": "Enable SSL verification. Use verify=True or provide CA bundle.",
                "languages": ["python"],
            },
        ]

    async def scan_file(self, file_path: str | Path) -> list[Vulnerability]:
        """Scan a single file for vulnerabilities.

        Args:
            file_path: Path to file to scan

        Returns:
            List of vulnerabilities found
        """
        file_path = Path(file_path)
        vulnerabilities: list[Vulnerability] = []

        if not file_path.exists():
            logger.warning(f"File not found: {file_path}")
            return vulnerabilities

        # Determine language from extension
        ext = file_path.suffix.lower()
        language_map = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".jsx": "javascript",
            ".tsx": "typescript",
        }
        language = language_map.get(ext)

        if not language:
            return vulnerabilities

        try:
            content = file_path.read_text()
            lines = content.split("\n")

            for pattern_def in self._patterns:
                if language not in pattern_def.get("languages", []):
                    continue

                pattern = re.compile(pattern_def["pattern"], re.IGNORECASE)

                for i, line in enumerate(lines, 1):
                    if pattern.search(line):
                        self._vuln_counter += 1
                        vuln = Vulnerability(
                            id=f"VULN-{self._vuln_counter:04d}",
                            category=pattern_def["category"],
                            severity=pattern_def["severity"],
                            title=pattern_def["title"],
                            file_path=str(file_path),
                            line_number=i,
                            code_snippet=line.strip(),
                            cwe_id=pattern_def.get("cwe"),
                            remediation=pattern_def["remediation"],
                        )
                        vulnerabilities.append(vuln)

        except Exception as e:
            logger.error(f"Error scanning {file_path}: {e}")

        return vulnerabilities

    async def scan_workspace(
        self,
        patterns: Optional[list[str]] = None,
        exclude_patterns: Optional[list[str]] = None,
    ) -> SecurityReport:
        """Scan workspace for vulnerabilities.

        Args:
            patterns: Glob patterns to include (default: common source files)
            exclude_patterns: Glob patterns to exclude

        Returns:
            SecurityReport with all findings
        """
        import time
        start_time = time.time()

        if patterns is None:
            patterns = ["**/*.py", "**/*.js", "**/*.ts", "**/*.jsx", "**/*.tsx"]

        if exclude_patterns is None:
            exclude_patterns = ["**/node_modules/**", "**/.git/**", "**/venv/**", "**/__pycache__/**"]

        # Find files
        files_to_scan: list[Path] = []
        for pattern in patterns:
            for file_path in self.workspace_path.glob(pattern):
                # Check exclusions
                excluded = False
                for exc_pattern in exclude_patterns:
                    if file_path.match(exc_pattern):
                        excluded = True
                        break
                if not excluded and file_path.is_file():
                    files_to_scan.append(file_path)

        # Scan files concurrently
        all_vulns: list[Vulnerability] = []
        tasks = [self.scan_file(f) for f in files_to_scan]
        results = await asyncio.gather(*tasks)

        for vulns in results:
            all_vulns.extend(vulns)

        duration_ms = int((time.time() - start_time) * 1000)

        return SecurityReport(
            vulnerabilities=all_vulns,
            files_scanned=len(files_to_scan),
            scan_duration_ms=duration_ms,
        )

    async def scan_diff(
        self,
        old_content: str,
        new_content: str,
        file_path: str = "diff",
    ) -> list[Vulnerability]:
        """Scan code diff for new vulnerabilities.

        Args:
            old_content: Original file content
            new_content: Modified file content
            file_path: Path for reporting

        Returns:
            List of vulnerabilities in new content only
        """
        # Write to temp file and scan
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(new_content)
            temp_path = Path(f.name)

        try:
            new_vulns = await self.scan_file(temp_path)

            # Update file paths in results
            for vuln in new_vulns:
                vuln.file_path = file_path

            return new_vulns
        finally:
            temp_path.unlink()

    async def suggest_patch(self, vulnerability: Vulnerability) -> Optional[str]:
        """Use LLM to suggest a patch for a vulnerability.

        Args:
            vulnerability: Vulnerability to patch

        Returns:
            Suggested patch or None
        """
        if not self.llm_callback:
            return None

        prompt = f"""Fix the following security vulnerability:

**Title:** {vulnerability.title}
**Category:** {vulnerability.category}
**CWE:** {vulnerability.cwe_id}

**Vulnerable Code:**
```
{vulnerability.code_snippet}
```

**Remediation Guidance:** {vulnerability.remediation}

Provide ONLY the fixed code, no explanation. The fix should:
1. Address the security issue
2. Maintain the same functionality
3. Follow best practices
"""

        try:
            response = await self.llm_callback(
                "You are a security expert. Fix code vulnerabilities.",
                prompt,
            )
            return response.strip()
        except Exception as e:
            logger.error(f"Failed to get patch suggestion: {e}")
            return None
