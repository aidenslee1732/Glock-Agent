"""Agent configuration and definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .base import AgentModelTier


@dataclass
class AgentConfig:
    """Configuration for an agent type.

    Attributes:
        name: Unique agent name (used as subagent_type)
        description: Human-readable description
        system_prompt_file: Path to system prompt file (relative to prompts/)
        allowed_tools: List of allowed tool names (None = all)
        read_only: If True, agent cannot use write/edit tools
        max_turns: Default maximum turns
        model_tier: Default model tier
        has_context_access: Can see conversation history
        category: Category for organization
        aliases: Alternative names for this agent
    """
    name: str
    description: str
    system_prompt_file: Optional[str] = None
    allowed_tools: Optional[list[str]] = None
    read_only: bool = False
    max_turns: int = 50
    model_tier: AgentModelTier = AgentModelTier.STANDARD
    has_context_access: bool = False
    category: str = "general"
    aliases: list[str] = field(default_factory=list)


# Core agents (always available)
CORE_AGENTS: dict[str, AgentConfig] = {
    # Exploration agent - fast codebase exploration
    "Explore": AgentConfig(
        name="Explore",
        description="Fast agent for exploring codebases. Use for finding files, searching code, or answering questions about the codebase.",
        system_prompt_file="explore.md",
        allowed_tools=["read_file", "glob", "grep", "list_directory"],
        read_only=True,
        max_turns=30,
        model_tier=AgentModelTier.FAST,
        has_context_access=True,
        category="core",
        aliases=["explore", "search", "find"],
    ),

    # Plan agent - architecture planning
    "Plan": AgentConfig(
        name="Plan",
        description="Software architect agent for designing implementation plans. Use for planning strategy, identifying critical files, and architectural decisions.",
        system_prompt_file="plan.md",
        allowed_tools=["read_file", "glob", "grep", "list_directory", "web_fetch", "web_search"],
        read_only=True,
        max_turns=50,
        model_tier=AgentModelTier.STANDARD,
        has_context_access=True,
        category="core",
        aliases=["plan", "architect", "design"],
    ),

    # Bash agent - command execution
    "Bash": AgentConfig(
        name="Bash",
        description="Command execution specialist for running bash commands. Use for git operations, command execution, and terminal tasks.",
        system_prompt_file="bash.md",
        allowed_tools=["bash"],
        read_only=False,
        max_turns=20,
        model_tier=AgentModelTier.FAST,
        has_context_access=True,
        category="core",
        aliases=["bash", "shell", "terminal"],
    ),

    # General purpose agent - multi-step tasks
    "general-purpose": AgentConfig(
        name="general-purpose",
        description="General-purpose agent for researching complex questions, searching for code, and executing multi-step tasks.",
        system_prompt_file="general_purpose.md",
        allowed_tools=None,  # All tools
        read_only=False,
        max_turns=100,
        model_tier=AgentModelTier.STANDARD,
        has_context_access=True,
        category="core",
        aliases=["general", "default", "multi-step"],
    ),
}

# Language specialist agents
LANGUAGE_AGENTS: dict[str, AgentConfig] = {
    "python-expert": AgentConfig(
        name="python-expert",
        description="Python expert for best practices, async, typing, and modern Python idioms.",
        system_prompt_file="languages/python.md",
        category="languages",
        aliases=["python", "py"],
    ),
    "typescript-expert": AgentConfig(
        name="typescript-expert",
        description="TypeScript expert for type systems, generics, and advanced TypeScript patterns.",
        system_prompt_file="languages/typescript.md",
        category="languages",
        aliases=["typescript", "ts"],
    ),
    "javascript-expert": AgentConfig(
        name="javascript-expert",
        description="JavaScript expert for ES6+, browser/Node.js, and modern JS patterns.",
        system_prompt_file="languages/javascript.md",
        category="languages",
        aliases=["javascript", "js"],
    ),
    "go-expert": AgentConfig(
        name="go-expert",
        description="Go expert for idioms, concurrency, modules, and Go best practices.",
        system_prompt_file="languages/go.md",
        category="languages",
        aliases=["go", "golang"],
    ),
    "rust-expert": AgentConfig(
        name="rust-expert",
        description="Rust expert for ownership, lifetimes, async, and Rust patterns.",
        system_prompt_file="languages/rust.md",
        category="languages",
        aliases=["rust", "rs"],
    ),
    "java-expert": AgentConfig(
        name="java-expert",
        description="Java expert for Spring, enterprise patterns, and modern Java.",
        system_prompt_file="languages/java.md",
        category="languages",
        aliases=["java"],
    ),
    "csharp-expert": AgentConfig(
        name="csharp-expert",
        description="C# expert for .NET, LINQ, async, and C# patterns.",
        system_prompt_file="languages/csharp.md",
        category="languages",
        aliases=["csharp", "cs", "dotnet"],
    ),
    "ruby-expert": AgentConfig(
        name="ruby-expert",
        description="Ruby expert for idioms, metaprogramming, and Ruby best practices.",
        system_prompt_file="languages/ruby.md",
        category="languages",
        aliases=["ruby", "rb"],
    ),
    "php-expert": AgentConfig(
        name="php-expert",
        description="PHP expert for Laravel, modern PHP, and web development.",
        system_prompt_file="languages/php.md",
        category="languages",
        aliases=["php"],
    ),
    "swift-expert": AgentConfig(
        name="swift-expert",
        description="Swift expert for iOS, Apple frameworks, and Swift patterns.",
        system_prompt_file="languages/swift.md",
        category="languages",
        aliases=["swift", "ios"],
    ),
    "kotlin-expert": AgentConfig(
        name="kotlin-expert",
        description="Kotlin expert for Android, coroutines, and Kotlin idioms.",
        system_prompt_file="languages/kotlin.md",
        category="languages",
        aliases=["kotlin", "android"],
    ),
    "cpp-expert": AgentConfig(
        name="cpp-expert",
        description="C++ expert for memory management, STL, and modern C++.",
        system_prompt_file="languages/cpp.md",
        category="languages",
        aliases=["cpp", "c++", "cplusplus"],
    ),
}

# Framework specialist agents
FRAMEWORK_AGENTS: dict[str, AgentConfig] = {
    "react-expert": AgentConfig(
        name="react-expert",
        description="React expert for hooks, state management, and React patterns.",
        system_prompt_file="frameworks/react.md",
        category="frameworks",
        aliases=["react"],
    ),
    "nextjs-expert": AgentConfig(
        name="nextjs-expert",
        description="Next.js expert for SSR, App Router, and Next.js patterns.",
        system_prompt_file="frameworks/nextjs.md",
        category="frameworks",
        aliases=["nextjs", "next"],
    ),
    "vue-expert": AgentConfig(
        name="vue-expert",
        description="Vue 3 expert for Composition API, Pinia, and Vue patterns.",
        system_prompt_file="frameworks/vue.md",
        category="frameworks",
        aliases=["vue", "vuejs"],
    ),
    "angular-expert": AgentConfig(
        name="angular-expert",
        description="Angular expert for RxJS, NgRx, and Angular patterns.",
        system_prompt_file="frameworks/angular.md",
        category="frameworks",
        aliases=["angular", "ng"],
    ),
    "node-expert": AgentConfig(
        name="node-expert",
        description="Node.js expert for Express, Fastify, and Node patterns.",
        system_prompt_file="frameworks/node.md",
        category="frameworks",
        aliases=["node", "nodejs", "express"],
    ),
    "django-expert": AgentConfig(
        name="django-expert",
        description="Django expert for DRF, ORM, and Django patterns.",
        system_prompt_file="frameworks/django.md",
        category="frameworks",
        aliases=["django"],
    ),
    "fastapi-expert": AgentConfig(
        name="fastapi-expert",
        description="FastAPI expert for Pydantic, async, and FastAPI patterns.",
        system_prompt_file="frameworks/fastapi.md",
        category="frameworks",
        aliases=["fastapi"],
    ),
    "rails-expert": AgentConfig(
        name="rails-expert",
        description="Ruby on Rails expert for ActiveRecord and Rails patterns.",
        system_prompt_file="frameworks/rails.md",
        category="frameworks",
        aliases=["rails", "rubyonrails"],
    ),
    "spring-expert": AgentConfig(
        name="spring-expert",
        description="Spring Boot expert for Spring Cloud and Spring patterns.",
        system_prompt_file="frameworks/spring.md",
        category="frameworks",
        aliases=["spring", "springboot"],
    ),
    "flutter-expert": AgentConfig(
        name="flutter-expert",
        description="Flutter expert for Dart, mobile cross-platform development.",
        system_prompt_file="frameworks/flutter.md",
        category="frameworks",
        aliases=["flutter", "dart"],
    ),
}

# Core development agents
CORE_DEV_AGENTS: dict[str, AgentConfig] = {
    "frontend": AgentConfig(
        name="frontend",
        description="Frontend UI/UX development specialist. Default stack: Next.js + TypeScript + Tailwind + shadcn/ui.",
        system_prompt_file="core/frontend.md",
        category="core-dev",
        aliases=["ui", "ux", "front-end"],
    ),
    "backend": AgentConfig(
        name="backend",
        description="Backend services & APIs specialist. Default stack: FastAPI + Python.",
        system_prompt_file="core/backend.md",
        category="core-dev",
        aliases=["api", "server", "back-end"],
    ),
    "database": AgentConfig(
        name="database",
        description="Database design, queries, and migrations specialist.",
        system_prompt_file="core/database.md",
        category="core-dev",
        aliases=["db", "sql", "data"],
    ),
    "terminal": AgentConfig(
        name="terminal",
        description="Shell commands, scripts, and CLI tools specialist.",
        system_prompt_file="core/terminal.md",
        allowed_tools=["bash", "read_file", "write_file", "glob", "grep"],
        category="core-dev",
        aliases=["cli", "shell-expert"],
    ),
    "fullstack": AgentConfig(
        name="fullstack",
        description="End-to-end feature implementation specialist. Default stack: Next.js + TypeScript + shadcn/ui (frontend) and FastAPI + Python (backend).",
        system_prompt_file="core/fullstack.md",
        category="core-dev",
        aliases=["full-stack"],
    ),
    "architect": AgentConfig(
        name="architect",
        description="System design & architecture specialist.",
        system_prompt_file="core/architect.md",
        read_only=True,
        model_tier=AgentModelTier.ADVANCED,
        category="core-dev",
        aliases=["system-design", "architecture"],
    ),
}

# Operations & DevOps agents
OPERATIONS_AGENTS: dict[str, AgentConfig] = {
    "devops": AgentConfig(
        name="devops",
        description="CI/CD, pipelines, and automation specialist.",
        system_prompt_file="operations/devops.md",
        category="operations",
        aliases=["cicd", "automation"],
    ),
    "docker-expert": AgentConfig(
        name="docker-expert",
        description="Containers, Dockerfile, and compose specialist.",
        system_prompt_file="operations/docker.md",
        category="operations",
        aliases=["docker", "container"],
    ),
    "kubernetes-expert": AgentConfig(
        name="kubernetes-expert",
        description="K8s, Helm, and operators specialist.",
        system_prompt_file="operations/kubernetes.md",
        category="operations",
        aliases=["kubernetes", "k8s", "helm"],
    ),
    "terraform-expert": AgentConfig(
        name="terraform-expert",
        description="Infrastructure as Code and Terraform specialist.",
        system_prompt_file="operations/terraform.md",
        category="operations",
        aliases=["terraform", "iac", "infrastructure"],
    ),
    "ansible-expert": AgentConfig(
        name="ansible-expert",
        description="Configuration management and Ansible specialist.",
        system_prompt_file="operations/ansible.md",
        category="operations",
        aliases=["ansible", "config-mgmt"],
    ),
    "monitoring": AgentConfig(
        name="monitoring",
        description="Observability, metrics, and alerting specialist.",
        system_prompt_file="operations/monitoring.md",
        category="operations",
        aliases=["observability", "metrics", "alerting"],
    ),
    "logging": AgentConfig(
        name="logging",
        description="Log aggregation and analysis specialist.",
        system_prompt_file="operations/logging.md",
        category="operations",
        aliases=["logs", "log-analysis"],
    ),
    "performance": AgentConfig(
        name="performance",
        description="Performance tuning and profiling specialist.",
        system_prompt_file="operations/performance.md",
        category="operations",
        aliases=["perf", "profiling", "optimization"],
    ),
}

# Data & ML agents
DATA_AGENTS: dict[str, AgentConfig] = {
    "data-analyst": AgentConfig(
        name="data-analyst",
        description="Data analysis, pandas, and visualization specialist.",
        system_prompt_file="data/analyst.md",
        category="data",
        aliases=["analyst", "pandas", "visualization"],
    ),
    "ml-engineer": AgentConfig(
        name="ml-engineer",
        description="Machine learning, scikit-learn, and PyTorch specialist.",
        system_prompt_file="data/ml.md",
        category="data",
        aliases=["ml", "machine-learning", "pytorch"],
    ),
    "data-pipeline": AgentConfig(
        name="data-pipeline",
        description="ETL, Airflow, and data engineering specialist.",
        system_prompt_file="data/pipeline.md",
        category="data",
        aliases=["etl", "airflow", "data-engineering"],
    ),
    "sql-expert": AgentConfig(
        name="sql-expert",
        description="SQL optimization and query tuning specialist.",
        system_prompt_file="data/sql.md",
        category="data",
        aliases=["sql"],
    ),
    "nosql-expert": AgentConfig(
        name="nosql-expert",
        description="MongoDB, Redis, and DynamoDB specialist.",
        system_prompt_file="data/nosql.md",
        category="data",
        aliases=["nosql", "mongodb", "redis"],
    ),
    "analytics": AgentConfig(
        name="analytics",
        description="Business analytics and dashboards specialist.",
        system_prompt_file="data/analytics.md",
        category="data",
        aliases=["business-analytics", "dashboards"],
    ),
}

# Cloud platform agents
CLOUD_AGENTS: dict[str, AgentConfig] = {
    "aws-expert": AgentConfig(
        name="aws-expert",
        description="AWS services, Lambda, EC2, S3 specialist.",
        system_prompt_file="cloud/aws.md",
        category="cloud",
        aliases=["aws", "amazon"],
    ),
    "gcp-expert": AgentConfig(
        name="gcp-expert",
        description="Google Cloud, BigQuery, Cloud Run specialist.",
        system_prompt_file="cloud/gcp.md",
        category="cloud",
        aliases=["gcp", "google-cloud"],
    ),
    "azure-expert": AgentConfig(
        name="azure-expert",
        description="Azure services, Functions, AKS specialist.",
        system_prompt_file="cloud/azure.md",
        category="cloud",
        aliases=["azure", "microsoft"],
    ),
    "serverless": AgentConfig(
        name="serverless",
        description="Serverless architectures specialist.",
        system_prompt_file="cloud/serverless.md",
        category="cloud",
        aliases=["lambda", "functions"],
    ),
    "cloud-cost": AgentConfig(
        name="cloud-cost",
        description="Cost optimization and FinOps specialist.",
        system_prompt_file="cloud/cost.md",
        category="cloud",
        aliases=["finops", "cost-optimization"],
    ),
    "multi-cloud": AgentConfig(
        name="multi-cloud",
        description="Multi-cloud strategies specialist.",
        system_prompt_file="cloud/multicloud.md",
        category="cloud",
        aliases=["hybrid-cloud"],
    ),
}

# Security agents
SECURITY_AGENTS: dict[str, AgentConfig] = {
    "security-auditor": AgentConfig(
        name="security-auditor",
        description="Security review and vulnerability scanning specialist.",
        system_prompt_file="security/auditor.md",
        read_only=True,
        category="security",
        aliases=["security", "audit", "vulnerability"],
    ),
    "auth-expert": AgentConfig(
        name="auth-expert",
        description="Authentication, OAuth, JWT, SSO specialist.",
        system_prompt_file="security/auth.md",
        category="security",
        aliases=["auth", "oauth", "jwt"],
    ),
    "crypto-expert": AgentConfig(
        name="crypto-expert",
        description="Cryptography, encryption, and hashing specialist.",
        system_prompt_file="security/crypto.md",
        category="security",
        aliases=["crypto", "encryption"],
    ),
    "devsecops": AgentConfig(
        name="devsecops",
        description="Security in CI/CD, SAST/DAST specialist.",
        system_prompt_file="security/devsecops.md",
        category="security",
        aliases=["sast", "dast"],
    ),
    "compliance": AgentConfig(
        name="compliance",
        description="GDPR, HIPAA, SOC2 compliance specialist.",
        system_prompt_file="security/compliance.md",
        read_only=True,
        category="security",
        aliases=["gdpr", "hipaa", "soc2"],
    ),
}

# Testing & QA agents
TESTING_AGENTS: dict[str, AgentConfig] = {
    "qa-tester": AgentConfig(
        name="qa-tester",
        description="Test planning and manual testing specialist.",
        system_prompt_file="testing/qa.md",
        category="testing",
        aliases=["qa", "tester"],
    ),
    "unit-test": AgentConfig(
        name="unit-test",
        description="Unit testing, mocking, and coverage specialist.",
        system_prompt_file="testing/unit.md",
        category="testing",
        aliases=["unit", "unittest"],
    ),
    "integration-test": AgentConfig(
        name="integration-test",
        description="Integration testing and test fixtures specialist.",
        system_prompt_file="testing/integration.md",
        category="testing",
        aliases=["integration"],
    ),
    "e2e-test": AgentConfig(
        name="e2e-test",
        description="End-to-end testing, Playwright, Cypress specialist.",
        system_prompt_file="testing/e2e.md",
        category="testing",
        aliases=["e2e", "playwright", "cypress"],
    ),
    "load-test": AgentConfig(
        name="load-test",
        description="Load testing, k6, JMeter specialist.",
        system_prompt_file="testing/load.md",
        category="testing",
        aliases=["load", "stress", "k6"],
    ),
}

# Documentation & Design agents
DOCS_AGENTS: dict[str, AgentConfig] = {
    "docs-writer": AgentConfig(
        name="docs-writer",
        description="Technical documentation and README specialist.",
        system_prompt_file="docs/writer.md",
        category="docs",
        aliases=["docs", "documentation", "readme"],
    ),
    "api-designer": AgentConfig(
        name="api-designer",
        description="API design, OpenAPI, REST/GraphQL specialist.",
        system_prompt_file="docs/api_designer.md",
        category="docs",
        aliases=["openapi", "swagger"],
    ),
    "ux-reviewer": AgentConfig(
        name="ux-reviewer",
        description="UX review and accessibility specialist.",
        system_prompt_file="docs/ux_reviewer.md",
        read_only=True,
        category="docs",
        aliases=["ux", "accessibility", "a11y"],
    ),
    "diagram-maker": AgentConfig(
        name="diagram-maker",
        description="Architecture diagrams and Mermaid specialist.",
        system_prompt_file="docs/diagram.md",
        category="docs",
        aliases=["diagram", "mermaid"],
    ),
}

# Tool specialists
TOOL_AGENTS: dict[str, AgentConfig] = {
    "git-expert": AgentConfig(
        name="git-expert",
        description="Git workflows, branching, and history specialist.",
        system_prompt_file="tools/git.md",
        allowed_tools=["bash", "read_file", "glob", "grep"],
        category="tools",
        aliases=["git", "version-control"],
    ),
    "graphql-expert": AgentConfig(
        name="graphql-expert",
        description="GraphQL schemas and resolvers specialist.",
        system_prompt_file="tools/graphql.md",
        category="tools",
        aliases=["graphql", "gql"],
    ),
    "grpc-expert": AgentConfig(
        name="grpc-expert",
        description="gRPC and Protocol Buffers specialist.",
        system_prompt_file="tools/grpc.md",
        category="tools",
        aliases=["grpc", "protobuf"],
    ),
    "websocket-expert": AgentConfig(
        name="websocket-expert",
        description="Real-time, WebSocket, Socket.io specialist.",
        system_prompt_file="tools/websocket.md",
        category="tools",
        aliases=["websocket", "ws", "socketio"],
    ),
    "regex-expert": AgentConfig(
        name="regex-expert",
        description="Regular expressions and parsing specialist.",
        system_prompt_file="tools/regex.md",
        category="tools",
        aliases=["regex", "regexp"],
    ),
    "shell-scripter": AgentConfig(
        name="shell-scripter",
        description="Bash/Zsh scripts and automation specialist.",
        system_prompt_file="tools/shell.md",
        allowed_tools=["bash", "read_file", "write_file", "glob", "grep"],
        category="tools",
        aliases=["scripts", "bash-expert"],
    ),
}

# AI & Prompts agents
AI_AGENTS: dict[str, AgentConfig] = {
    "prompt-engineer": AgentConfig(
        name="prompt-engineer",
        description="Prompt design and optimization specialist.",
        system_prompt_file="ai/prompt.md",
        category="ai",
        aliases=["prompt", "prompts"],
    ),
    "rag-expert": AgentConfig(
        name="rag-expert",
        description="RAG systems and embeddings specialist.",
        system_prompt_file="ai/rag.md",
        category="ai",
        aliases=["rag", "embeddings"],
    ),
    "llm-integrator": AgentConfig(
        name="llm-integrator",
        description="LLM API integration specialist.",
        system_prompt_file="ai/llm.md",
        category="ai",
        aliases=["llm", "ai-integration"],
    ),
    "ai-safety": AgentConfig(
        name="ai-safety",
        description="AI safety and guardrails specialist.",
        system_prompt_file="ai/safety.md",
        read_only=True,
        category="ai",
        aliases=["safety", "guardrails"],
    ),
}


def get_all_agents() -> dict[str, AgentConfig]:
    """Get all available agent configurations."""
    all_agents = {}
    all_agents.update(CORE_AGENTS)
    all_agents.update(LANGUAGE_AGENTS)
    all_agents.update(FRAMEWORK_AGENTS)
    all_agents.update(CORE_DEV_AGENTS)
    all_agents.update(OPERATIONS_AGENTS)
    all_agents.update(DATA_AGENTS)
    all_agents.update(CLOUD_AGENTS)
    all_agents.update(SECURITY_AGENTS)
    all_agents.update(TESTING_AGENTS)
    all_agents.update(DOCS_AGENTS)
    all_agents.update(TOOL_AGENTS)
    all_agents.update(AI_AGENTS)
    return all_agents


def get_agent_by_alias(alias: str) -> Optional[AgentConfig]:
    """Get an agent config by alias.

    Args:
        alias: Agent name or alias

    Returns:
        AgentConfig if found, None otherwise
    """
    alias_lower = alias.lower()
    all_agents = get_all_agents()

    # Direct match
    if alias in all_agents:
        return all_agents[alias]

    # Alias match
    for config in all_agents.values():
        if alias_lower in [a.lower() for a in config.aliases]:
            return config
        if alias_lower == config.name.lower():
            return config

    return None
