"""LLM configuration loader from environment variables."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..planner.llm.gateway import LLMConfig


def load_llm_config() -> "LLMConfig":
    """Load LLM configuration from environment variables.

    Environment Variables:
        LLM_PROVIDER_MODE: Provider mode - "direct", "bedrock", or "hybrid" (default: "direct")
        AWS_REGION_NAME: AWS region for Bedrock (default: "us-east-1")
        AWS_ACCESS_KEY_ID: AWS access key for Bedrock
        AWS_SECRET_ACCESS_KEY: AWS secret key for Bedrock
        MODEL_TIER_FAST: Model for fast tier
        MODEL_TIER_STANDARD: Model for standard tier
        MODEL_TIER_ADVANCED: Model for advanced tier
        MODEL_TIER_REASONING: Model for reasoning tier
        BEDROCK_MODEL_FAST: Bedrock model for fast tier
        BEDROCK_MODEL_STANDARD: Bedrock model for standard tier
        BEDROCK_MODEL_ADVANCED: Bedrock model for advanced tier
        BEDROCK_MODEL_REASONING: Bedrock model for reasoning tier

    Returns:
        LLMConfig: Configured LLM settings
    """
    # Import here to avoid circular imports
    from ..planner.llm.gateway import LLMConfig

    return LLMConfig(
        provider_mode=os.environ.get("LLM_PROVIDER_MODE", "direct"),
        aws_region=os.environ.get("AWS_REGION_NAME", "us-east-1"),
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        tier_models={
            "fast": os.environ.get("MODEL_TIER_FAST", "claude-3-haiku-20240307"),
            "standard": os.environ.get("MODEL_TIER_STANDARD", "gpt-5.3-codex"),
            "advanced": os.environ.get("MODEL_TIER_ADVANCED", "gpt-5.3-codex"),
            "reasoning": os.environ.get("MODEL_TIER_REASONING", "gpt-5.3-codex"),
        },
        bedrock_tier_models={
            "fast": os.environ.get(
                "BEDROCK_MODEL_FAST", "bedrock/anthropic.claude-3-haiku-20240307-v1:0"
            ),
            "standard": os.environ.get(
                "BEDROCK_MODEL_STANDARD", "bedrock/anthropic.claude-opus-4-6-v1"
            ),
            "advanced": os.environ.get(
                "BEDROCK_MODEL_ADVANCED", "bedrock/anthropic.claude-opus-4-6-v1"
            ),
            "reasoning": os.environ.get(
                "BEDROCK_MODEL_REASONING", "bedrock/anthropic.claude-opus-4-6-v1"
            ),
        },
    )
