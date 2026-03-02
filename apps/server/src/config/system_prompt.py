"""System prompt configuration for Glock server.

Bug fix 1.8: Makes system prompt configurable via environment variables
or configuration files instead of being hardcoded.

Load order (first found wins):
1. GLOCK_SYSTEM_PROMPT environment variable (inline text)
2. GLOCK_SYSTEM_PROMPT_FILE environment variable (path to file)
3. ~/.glock/system_prompt.md
4. Default built-in prompt
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_SYSTEM_PROMPT = """You are Glock, an AI coding assistant. You help users build software projects.

## CRITICAL: Response Guidelines

1. **ANSWER QUESTIONS DIRECTLY** - When users ask questions, provide clear answers. Don't just run commands endlessly.
2. **LIMIT TOOL USAGE** - For questions, use 3-5 targeted tool calls max. If you can answer from context, do so without tools.
3. **PROVIDE VALUE** - Every response should give the user useful information or complete a task.
4. **STOP AND ANSWER** - After gathering info, STOP running commands and provide your answer.

## Default Technology Stack

When creating new projects, use these defaults unless the user specifies otherwise:

- **Frontend**: Next.js 14+ with TypeScript, Tailwind CSS, and shadcn/ui
- **Backend**: FastAPI with Python 3.11+
- **Fullstack**: Both of the above with CORS pre-configured

Always use the default stack for new projects. Only deviate if the user explicitly requests a different technology.

## Project Creation Guidelines

When creating new projects, ALWAYS use proper CLI tools:

### Frontend (Next.js)
```bash
npx create-next-app@latest <project-name> --typescript --tailwind --eslint --app --src-dir --import-alias "@/*" --use-npm
```
Then add shadcn/ui:
```bash
cd <project-name> && npx shadcn@latest init -y && npx shadcn@latest add button input card
```

### Backend (FastAPI)
Create the directory and write main.py with FastAPI app including CORS middleware for localhost:3000.

## Code Guidelines

- Write clean, production-ready code
- Follow best practices for each technology
- Include proper error handling
- Add helpful comments where needed
- Create complete, working implementations
- ALWAYS wait for CLI commands to complete before proceeding"""


@dataclass
class SystemPromptConfig:
    """Configuration for system prompt."""

    prompt: str
    source: str  # Where the prompt was loaded from

    @classmethod
    def from_env(cls) -> Optional["SystemPromptConfig"]:
        """Load from environment variable (inline)."""
        prompt = os.environ.get("GLOCK_SYSTEM_PROMPT")
        if prompt:
            return cls(prompt=prompt, source="env:GLOCK_SYSTEM_PROMPT")
        return None

    @classmethod
    def from_env_file(cls) -> Optional["SystemPromptConfig"]:
        """Load from file specified in environment variable."""
        prompt_file = os.environ.get("GLOCK_SYSTEM_PROMPT_FILE")
        if prompt_file:
            path = Path(prompt_file)
            if path.exists():
                try:
                    prompt = path.read_text()
                    return cls(prompt=prompt, source=f"env_file:{prompt_file}")
                except Exception as e:
                    logger.warning(f"Failed to read system prompt from {prompt_file}: {e}")
        return None

    @classmethod
    def from_user_config(cls) -> Optional["SystemPromptConfig"]:
        """Load from user's home config directory."""
        config_path = Path.home() / ".glock" / "system_prompt.md"
        if config_path.exists():
            try:
                prompt = config_path.read_text()
                return cls(prompt=prompt, source=f"user_config:{config_path}")
            except Exception as e:
                logger.warning(f"Failed to read system prompt from {config_path}: {e}")
        return None

    @classmethod
    def default(cls) -> "SystemPromptConfig":
        """Get default system prompt."""
        return cls(prompt=DEFAULT_SYSTEM_PROMPT, source="default")


def load_system_prompt() -> SystemPromptConfig:
    """Load system prompt from configuration.

    Load order (first found wins):
    1. GLOCK_SYSTEM_PROMPT environment variable (inline text)
    2. GLOCK_SYSTEM_PROMPT_FILE environment variable (path to file)
    3. ~/.glock/system_prompt.md
    4. Default built-in prompt

    Returns:
        SystemPromptConfig with the loaded prompt and source info
    """
    # Try each source in order
    loaders = [
        SystemPromptConfig.from_env,
        SystemPromptConfig.from_env_file,
        SystemPromptConfig.from_user_config,
    ]

    for loader in loaders:
        config = loader()
        if config:
            logger.info(f"Loaded system prompt from {config.source}")
            return config

    # Fall back to default
    config = SystemPromptConfig.default()
    logger.debug(f"Using default system prompt")
    return config
