"""Agent registry for lazy loading and management."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Type

from .base import BaseAgent, AgentContext, AgentModelTier
from .config import AgentConfig, get_all_agents, get_agent_by_alias

logger = logging.getLogger(__name__)


class DynamicAgent(BaseAgent):
    """Dynamically created agent from AgentConfig."""

    def __init__(self, config: AgentConfig, prompts_dir: Path):
        super().__init__()
        self._config = config
        self._prompts_dir = prompts_dir

        # Set attributes from config
        self.name = config.name
        self.description = config.description
        self.allowed_tools = config.allowed_tools
        self.read_only = config.read_only
        self.max_turns = config.max_turns
        self.model_tier = config.model_tier
        self.has_context_access = config.has_context_access

        # Load system prompt
        self.system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        """Load system prompt from file or generate default."""
        if self._config.system_prompt_file:
            prompt_path = self._prompts_dir / self._config.system_prompt_file
            if prompt_path.exists():
                return prompt_path.read_text()
            else:
                logger.warning(f"Prompt file not found: {prompt_path}")

        # Generate default prompt
        return self._generate_default_prompt()

    def _generate_default_prompt(self) -> str:
        """Generate a default system prompt for this agent."""
        return f"""You are {self.name}, a specialized agent.

{self.description}

Focus on your area of expertise and provide accurate, helpful responses.
When you don't know something, say so clearly.
Always prefer practical, working solutions over theoretical explanations.
"""


class AgentRegistry:
    """Registry for managing available agents.

    Supports lazy loading of agents and aliased lookups.
    """

    def __init__(self, prompts_dir: Optional[str] = None):
        """Initialize the registry.

        Args:
            prompts_dir: Directory containing agent prompt files
        """
        if prompts_dir:
            self._prompts_dir = Path(prompts_dir)
        else:
            # Default to prompts/ relative to this file
            self._prompts_dir = Path(__file__).parent / "prompts"

        # Load all agent configs
        self._configs = get_all_agents()

        # Cache for instantiated agents
        self._agent_cache: Dict[str, BaseAgent] = {}

        # Custom agent classes (for specialized implementations)
        self._custom_classes: Dict[str, Type[BaseAgent]] = {}

    def register_custom_class(self, name: str, agent_class: Type[BaseAgent]) -> None:
        """Register a custom agent class.

        Args:
            name: Agent name
            agent_class: Custom BaseAgent subclass
        """
        self._custom_classes[name] = agent_class

    def get(self, name: str) -> Optional[BaseAgent]:
        """Get an agent by name or alias.

        Args:
            name: Agent name or alias

        Returns:
            Agent instance, or None if not found
        """
        # Check cache first
        if name in self._agent_cache:
            return self._agent_cache[name]

        # Find config
        config = self._configs.get(name) or get_agent_by_alias(name)
        if not config:
            logger.warning(f"Agent not found: {name}")
            return None

        # Create agent instance
        agent = self._create_agent(config)
        if agent:
            self._agent_cache[config.name] = agent
            # Also cache by original lookup name
            if name != config.name:
                self._agent_cache[name] = agent

        return agent

    def _create_agent(self, config: AgentConfig) -> BaseAgent:
        """Create an agent instance from config.

        Args:
            config: Agent configuration

        Returns:
            Agent instance
        """
        # Check for custom class
        if config.name in self._custom_classes:
            agent_class = self._custom_classes[config.name]
            agent = agent_class()
            # Override attributes from config
            agent.name = config.name
            agent.description = config.description
            if config.allowed_tools:
                agent.allowed_tools = config.allowed_tools
            agent.read_only = config.read_only
            agent.max_turns = config.max_turns
            agent.model_tier = config.model_tier
            return agent

        # Use dynamic agent
        return DynamicAgent(config, self._prompts_dir)

    def list_agents(self, category: Optional[str] = None) -> list[AgentConfig]:
        """List available agents.

        Args:
            category: Optional category filter

        Returns:
            List of agent configs
        """
        configs = list(self._configs.values())

        if category:
            configs = [c for c in configs if c.category == category]

        return sorted(configs, key=lambda c: c.name)

    def list_categories(self) -> list[str]:
        """List available agent categories.

        Returns:
            List of category names
        """
        categories = set(c.category for c in self._configs.values())
        return sorted(categories)

    def search(self, query: str) -> list[AgentConfig]:
        """Search for agents by name, description, or alias.

        Args:
            query: Search query

        Returns:
            List of matching agent configs
        """
        query_lower = query.lower()
        matches = []

        for config in self._configs.values():
            # Check name
            if query_lower in config.name.lower():
                matches.append(config)
                continue

            # Check description
            if query_lower in config.description.lower():
                matches.append(config)
                continue

            # Check aliases
            if any(query_lower in alias.lower() for alias in config.aliases):
                matches.append(config)
                continue

        return sorted(matches, key=lambda c: c.name)

    def get_config(self, name: str) -> Optional[AgentConfig]:
        """Get agent configuration by name.

        Args:
            name: Agent name or alias

        Returns:
            AgentConfig if found
        """
        return self._configs.get(name) or get_agent_by_alias(name)

    def ensure_prompts_dir(self) -> None:
        """Ensure prompts directory exists with subdirectories."""
        self._prompts_dir.mkdir(parents=True, exist_ok=True)

        # Create category subdirectories
        for category in self.list_categories():
            category_dir = self._prompts_dir / category
            category_dir.mkdir(exist_ok=True)
