"""
Security gate for Glock server.

Performs security analysis on:
- Task prompts for injection attempts
- Tool requests for dangerous operations
- Code changes for vulnerability patterns
- Workspace access for sensitive files
"""

import re
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Any, Set, Pattern
from pathlib import Path


logger = logging.getLogger(__name__)


class RiskLevel(Enum):
    """Risk levels for security assessment."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ThreatCategory(Enum):
    """Categories of security threats."""
    PROMPT_INJECTION = "prompt_injection"
    PATH_TRAVERSAL = "path_traversal"
    COMMAND_INJECTION = "command_injection"
    CREDENTIAL_EXPOSURE = "credential_exposure"
    SENSITIVE_FILE_ACCESS = "sensitive_file_access"
    DANGEROUS_OPERATION = "dangerous_operation"
    CODE_VULNERABILITY = "code_vulnerability"
    SUPPLY_CHAIN = "supply_chain"


@dataclass
class SecurityFinding:
    """A security finding from analysis."""
    category: ThreatCategory
    risk_level: RiskLevel
    description: str
    location: Optional[str] = None
    evidence: Optional[str] = None
    recommendation: Optional[str] = None


@dataclass
class SecurityAssessment:
    """Result of security assessment."""
    allowed: bool
    risk_level: RiskLevel
    findings: List[SecurityFinding] = field(default_factory=list)
    requires_approval: bool = False
    blocked_reason: Optional[str] = None

    def add_finding(self, finding: SecurityFinding) -> None:
        """Add a finding and update risk level."""
        self.findings.append(finding)
        if finding.risk_level.value > self.risk_level.value:
            self.risk_level = finding.risk_level


@dataclass
class SecurityConfig:
    """Configuration for security gate."""
    # Blocking thresholds
    block_on_critical: bool = True
    block_on_high: bool = False
    require_approval_on_high: bool = True

    # Feature toggles
    check_prompt_injection: bool = True
    check_path_traversal: bool = True
    check_command_injection: bool = True
    check_credentials: bool = True
    check_sensitive_files: bool = True
    check_code_vulnerabilities: bool = True

    # Custom patterns
    additional_sensitive_patterns: List[str] = field(default_factory=list)
    allowed_dangerous_commands: List[str] = field(default_factory=list)


class SecurityGate:
    """
    Security gate for validating operations.

    Checks:
    - Prompt injection attempts
    - Path traversal attacks
    - Command injection
    - Credential exposure
    - Sensitive file access
    - Dangerous operations
    - Code vulnerabilities
    """

    # Prompt injection patterns
    INJECTION_PATTERNS: List[Pattern] = [
        re.compile(r'ignore\s+(previous|all)\s+instructions', re.IGNORECASE),
        re.compile(r'disregard\s+(previous|all|your)\s+(instructions|rules)', re.IGNORECASE),
        re.compile(r'you\s+are\s+now\s+[a-z]+', re.IGNORECASE),
        re.compile(r'pretend\s+to\s+be', re.IGNORECASE),
        re.compile(r'act\s+as\s+if\s+you\s+are', re.IGNORECASE),
        re.compile(r'system\s*:\s*', re.IGNORECASE),
        re.compile(r'\[SYSTEM\]', re.IGNORECASE),
        re.compile(r'<\|system\|>', re.IGNORECASE),
        re.compile(r'###\s*SYSTEM', re.IGNORECASE),
        re.compile(r'forget\s+everything', re.IGNORECASE),
        re.compile(r'new\s+instructions:', re.IGNORECASE),
        re.compile(r'override\s+(security|safety)', re.IGNORECASE),
    ]

    # Path traversal patterns
    PATH_TRAVERSAL_PATTERNS: List[Pattern] = [
        re.compile(r'\.\.[\\/]'),
        re.compile(r'%2e%2e[\\/]', re.IGNORECASE),
        re.compile(r'%252e%252e', re.IGNORECASE),
        re.compile(r'\.\.%c0%af', re.IGNORECASE),
        re.compile(r'\.\.%c1%9c', re.IGNORECASE),
    ]

    # Sensitive file patterns
    SENSITIVE_FILE_PATTERNS: List[Pattern] = [
        re.compile(r'\.env($|\.)'),
        re.compile(r'\.env\.local'),
        re.compile(r'\.env\.(dev|prod|staging)'),
        re.compile(r'credentials\.json'),
        re.compile(r'secrets\.ya?ml'),
        re.compile(r'\.aws/credentials'),
        re.compile(r'\.ssh/'),
        re.compile(r'id_rsa'),
        re.compile(r'id_ed25519'),
        re.compile(r'\.pem$'),
        re.compile(r'\.key$'),
        re.compile(r'private.*key', re.IGNORECASE),
        re.compile(r'\.kube/config'),
        re.compile(r'\.docker/config\.json'),
        re.compile(r'\.npmrc'),
        re.compile(r'\.pypirc'),
        re.compile(r'\.netrc'),
        re.compile(r'htpasswd'),
        re.compile(r'shadow$'),
        re.compile(r'passwd$'),
    ]

    # Credential patterns in content
    CREDENTIAL_PATTERNS: List[Pattern] = [
        re.compile(r'(?:api[_-]?key|apikey)\s*[=:]\s*["\']?[\w-]{20,}', re.IGNORECASE),
        re.compile(r'(?:secret|password|passwd|pwd)\s*[=:]\s*["\']?[^\s"\',]{8,}', re.IGNORECASE),
        re.compile(r'(?:token)\s*[=:]\s*["\']?[\w-]{20,}', re.IGNORECASE),
        re.compile(r'Bearer\s+[\w-]{20,}'),
        re.compile(r'(?:AKIA|ABIA|ACCA|ASIA)[A-Z0-9]{16}'),  # AWS access key
        re.compile(r'sk-[a-zA-Z0-9]{32,}'),  # OpenAI API key
        re.compile(r'sk-ant-[a-zA-Z0-9-]{32,}'),  # Anthropic API key
        re.compile(r'xox[baprs]-[\w-]+'),  # Slack tokens
        re.compile(r'ghp_[a-zA-Z0-9]{36}'),  # GitHub PAT
        re.compile(r'gho_[a-zA-Z0-9]{36}'),  # GitHub OAuth
        re.compile(r'github_pat_[a-zA-Z0-9]{22}_[a-zA-Z0-9]{59}'),  # GitHub fine-grained PAT
    ]

    # Dangerous commands
    DANGEROUS_COMMANDS: Set[str] = {
        'rm -rf', 'rm -r /',
        'chmod 777', 'chmod -R 777',
        'curl.*\|.*sh', 'wget.*\|.*sh',
        ':(){:|:&};:',  # Fork bomb
        'dd if=', 'mkfs',
        '> /dev/sda', '> /dev/hda',
        'shutdown', 'reboot', 'init 0', 'init 6',
        'iptables -F', 'ufw disable',
        'passwd', 'useradd', 'userdel',
        'visudo', 'sudoers',
    }

    # SQL injection patterns
    SQL_INJECTION_PATTERNS: List[Pattern] = [
        re.compile(r"['\"];\s*(?:DROP|DELETE|UPDATE|INSERT)", re.IGNORECASE),
        re.compile(r"UNION\s+(?:ALL\s+)?SELECT", re.IGNORECASE),
        re.compile(r"OR\s+['\"]?1['\"]?\s*=\s*['\"]?1", re.IGNORECASE),
        re.compile(r"--\s*$", re.MULTILINE),
    ]

    # XSS patterns
    XSS_PATTERNS: List[Pattern] = [
        re.compile(r'<script[^>]*>', re.IGNORECASE),
        re.compile(r'javascript:', re.IGNORECASE),
        re.compile(r'on\w+\s*=', re.IGNORECASE),
        re.compile(r'<iframe', re.IGNORECASE),
    ]

    def __init__(self, config: Optional[SecurityConfig] = None):
        self.config = config or SecurityConfig()

        # Compile additional patterns
        self._additional_sensitive: List[Pattern] = [
            re.compile(p) for p in self.config.additional_sensitive_patterns
        ]

    def assess_prompt(self, prompt: str) -> SecurityAssessment:
        """Assess prompt for security issues."""
        assessment = SecurityAssessment(
            allowed=True,
            risk_level=RiskLevel.LOW
        )

        if self.config.check_prompt_injection:
            self._check_prompt_injection(prompt, assessment)

        self._finalize_assessment(assessment)
        return assessment

    def assess_tool_request(
        self,
        tool_name: str,
        args: Dict[str, Any],
        workspace_root: Optional[str] = None
    ) -> SecurityAssessment:
        """Assess tool request for security issues."""
        assessment = SecurityAssessment(
            allowed=True,
            risk_level=RiskLevel.LOW
        )

        # Check by tool type
        if tool_name == "bash":
            self._check_bash_command(args.get("command", ""), assessment)

        elif tool_name in ("read_file", "edit_file", "write_file"):
            path = args.get("path", "")
            if self.config.check_path_traversal:
                self._check_path_traversal(path, assessment)
            if self.config.check_sensitive_files:
                self._check_sensitive_file(path, assessment)

            # Check content for credentials
            if tool_name in ("edit_file", "write_file"):
                content = args.get("content", "") or args.get("new_string", "")
                if self.config.check_credentials:
                    self._check_credential_exposure(content, assessment)

        elif tool_name == "glob" or tool_name == "grep":
            pattern = args.get("pattern", "")
            if self.config.check_sensitive_files:
                self._check_sensitive_pattern(pattern, assessment)

        self._finalize_assessment(assessment)
        return assessment

    def assess_code_change(
        self,
        file_path: str,
        old_content: str,
        new_content: str
    ) -> SecurityAssessment:
        """Assess code change for vulnerabilities."""
        assessment = SecurityAssessment(
            allowed=True,
            risk_level=RiskLevel.LOW
        )

        if not self.config.check_code_vulnerabilities:
            return assessment

        # Check for introduced vulnerabilities
        self._check_sql_injection(new_content, assessment)
        self._check_xss(new_content, assessment)
        self._check_credential_exposure(new_content, assessment)

        # Check for removed security measures
        self._check_removed_security(old_content, new_content, assessment)

        self._finalize_assessment(assessment)
        return assessment

    def _check_prompt_injection(
        self,
        prompt: str,
        assessment: SecurityAssessment
    ) -> None:
        """Check for prompt injection attempts."""
        for pattern in self.INJECTION_PATTERNS:
            match = pattern.search(prompt)
            if match:
                assessment.add_finding(SecurityFinding(
                    category=ThreatCategory.PROMPT_INJECTION,
                    risk_level=RiskLevel.HIGH,
                    description="Potential prompt injection detected",
                    evidence=match.group(0),
                    recommendation="Review prompt for malicious instructions"
                ))

    def _check_path_traversal(
        self,
        path: str,
        assessment: SecurityAssessment
    ) -> None:
        """Check for path traversal attempts."""
        for pattern in self.PATH_TRAVERSAL_PATTERNS:
            if pattern.search(path):
                assessment.add_finding(SecurityFinding(
                    category=ThreatCategory.PATH_TRAVERSAL,
                    risk_level=RiskLevel.CRITICAL,
                    description="Path traversal attempt detected",
                    location=path,
                    recommendation="Use absolute paths within workspace"
                ))

    def _check_sensitive_file(
        self,
        path: str,
        assessment: SecurityAssessment
    ) -> None:
        """Check for access to sensitive files."""
        path_lower = path.lower()

        for pattern in self.SENSITIVE_FILE_PATTERNS + self._additional_sensitive:
            if pattern.search(path_lower):
                assessment.add_finding(SecurityFinding(
                    category=ThreatCategory.SENSITIVE_FILE_ACCESS,
                    risk_level=RiskLevel.HIGH,
                    description="Access to potentially sensitive file",
                    location=path,
                    recommendation="Verify this file should be accessed"
                ))
                break

    def _check_sensitive_pattern(
        self,
        pattern: str,
        assessment: SecurityAssessment
    ) -> None:
        """Check if search pattern targets sensitive files."""
        # Check if pattern explicitly targets sensitive files
        sensitive_targets = ['.env', 'secret', 'credential', 'password', 'key', '.pem']
        pattern_lower = pattern.lower()

        for target in sensitive_targets:
            if target in pattern_lower:
                assessment.add_finding(SecurityFinding(
                    category=ThreatCategory.SENSITIVE_FILE_ACCESS,
                    risk_level=RiskLevel.MEDIUM,
                    description="Search pattern may target sensitive files",
                    evidence=pattern,
                    recommendation="Review search intent"
                ))
                break

    def _check_credential_exposure(
        self,
        content: str,
        assessment: SecurityAssessment
    ) -> None:
        """Check for credential exposure in content."""
        for pattern in self.CREDENTIAL_PATTERNS:
            match = pattern.search(content)
            if match:
                # Mask the credential in evidence
                masked = self._mask_credential(match.group(0))
                assessment.add_finding(SecurityFinding(
                    category=ThreatCategory.CREDENTIAL_EXPOSURE,
                    risk_level=RiskLevel.CRITICAL,
                    description="Potential credential detected in content",
                    evidence=masked,
                    recommendation="Remove credentials and use environment variables"
                ))

    def _mask_credential(self, credential: str) -> str:
        """Mask a credential for safe logging."""
        if len(credential) <= 8:
            return "*" * len(credential)
        return credential[:4] + "*" * (len(credential) - 8) + credential[-4:]

    def _check_bash_command(
        self,
        command: str,
        assessment: SecurityAssessment
    ) -> None:
        """Check bash command for dangerous operations."""
        command_lower = command.lower()

        # Check for command injection
        if self.config.check_command_injection:
            injection_chars = ['`', '$(', '${', '|', '&&', '||', ';']
            for char in injection_chars:
                if char in command:
                    assessment.add_finding(SecurityFinding(
                        category=ThreatCategory.COMMAND_INJECTION,
                        risk_level=RiskLevel.MEDIUM,
                        description=f"Command contains potential injection character: {char}",
                        evidence=command[:100],
                        recommendation="Review command for unintended execution"
                    ))

        # Check for dangerous commands
        for dangerous in self.DANGEROUS_COMMANDS:
            if dangerous in command_lower:
                # Check if it's allowed
                if dangerous in self.config.allowed_dangerous_commands:
                    continue

                assessment.add_finding(SecurityFinding(
                    category=ThreatCategory.DANGEROUS_OPERATION,
                    risk_level=RiskLevel.CRITICAL,
                    description=f"Dangerous command pattern detected: {dangerous}",
                    evidence=command[:100],
                    recommendation="This operation requires explicit approval"
                ))

        # Check for credential access
        if self.config.check_credentials:
            cred_access_patterns = [
                r'cat\s+.*\.env',
                r'cat\s+.*credentials',
                r'echo\s+\$\{?[A-Z_]*(?:KEY|SECRET|TOKEN|PASSWORD)',
                r'printenv',
                r'env\s*$',
            ]
            for pattern in cred_access_patterns:
                if re.search(pattern, command, re.IGNORECASE):
                    assessment.add_finding(SecurityFinding(
                        category=ThreatCategory.CREDENTIAL_EXPOSURE,
                        risk_level=RiskLevel.HIGH,
                        description="Command may expose credentials",
                        evidence=command[:100],
                        recommendation="Avoid exposing credentials in command output"
                    ))

    def _check_sql_injection(
        self,
        content: str,
        assessment: SecurityAssessment
    ) -> None:
        """Check for SQL injection vulnerabilities."""
        for pattern in self.SQL_INJECTION_PATTERNS:
            match = pattern.search(content)
            if match:
                assessment.add_finding(SecurityFinding(
                    category=ThreatCategory.CODE_VULNERABILITY,
                    risk_level=RiskLevel.HIGH,
                    description="Potential SQL injection vulnerability",
                    evidence=match.group(0),
                    recommendation="Use parameterized queries"
                ))

    def _check_xss(
        self,
        content: str,
        assessment: SecurityAssessment
    ) -> None:
        """Check for XSS vulnerabilities."""
        for pattern in self.XSS_PATTERNS:
            match = pattern.search(content)
            if match:
                assessment.add_finding(SecurityFinding(
                    category=ThreatCategory.CODE_VULNERABILITY,
                    risk_level=RiskLevel.HIGH,
                    description="Potential XSS vulnerability",
                    evidence=match.group(0),
                    recommendation="Sanitize user input before rendering"
                ))

    def _check_removed_security(
        self,
        old_content: str,
        new_content: str,
        assessment: SecurityAssessment
    ) -> None:
        """Check if security measures were removed."""
        security_patterns = [
            (r'escape\(', "HTML escaping"),
            (r'sanitize', "Sanitization"),
            (r'validate', "Validation"),
            (r'parameterized', "Parameterized query"),
            (r'prepared\s*statement', "Prepared statement"),
            (r'csrf', "CSRF protection"),
            (r'xss', "XSS protection"),
        ]

        for pattern, description in security_patterns:
            old_matches = len(re.findall(pattern, old_content, re.IGNORECASE))
            new_matches = len(re.findall(pattern, new_content, re.IGNORECASE))

            if old_matches > 0 and new_matches < old_matches:
                assessment.add_finding(SecurityFinding(
                    category=ThreatCategory.CODE_VULNERABILITY,
                    risk_level=RiskLevel.MEDIUM,
                    description=f"Security measure may have been removed: {description}",
                    recommendation="Verify security controls are still in place"
                ))

    def _finalize_assessment(self, assessment: SecurityAssessment) -> None:
        """Finalize assessment based on findings."""
        if not assessment.findings:
            return

        # Determine highest risk level
        max_risk = max(f.risk_level for f in assessment.findings)
        assessment.risk_level = max_risk

        # Determine if blocked
        if self.config.block_on_critical and max_risk == RiskLevel.CRITICAL:
            assessment.allowed = False
            assessment.blocked_reason = "Critical security issue detected"
        elif self.config.block_on_high and max_risk == RiskLevel.HIGH:
            assessment.allowed = False
            assessment.blocked_reason = "High-risk security issue detected"

        # Determine if approval required
        if max_risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            if self.config.require_approval_on_high:
                assessment.requires_approval = True
