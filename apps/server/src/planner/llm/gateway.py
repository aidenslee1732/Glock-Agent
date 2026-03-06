"""
LLM Gateway for Glock server.

Provides unified access to LLM providers through LiteLLM,
with support for:
- Multiple providers (Anthropic, OpenAI, Google)
- Model tier routing
- Usage tracking
- Error handling and retries
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from enum import Enum
from typing import Any, Optional, AsyncIterator

from pydantic import BaseModel, Field, model_validator

try:
    import litellm
    from litellm import acompletion
    LITELLM_AVAILABLE = True
    # Allow LiteLLM to modify params when messages contain tool_calls but no tools provided
    # This prevents "Anthropic doesn't support tool calling without tools= param" errors
    litellm.modify_params = True
except ImportError:
    LITELLM_AVAILABLE = False

from ...metering.events import emit_usage_event


logger = logging.getLogger(__name__)


class ModelTier(Enum):
    """Model tiers for routing."""
    FAST = "fast"
    STANDARD = "standard"
    ADVANCED = "advanced"
    REASONING = "reasoning"


class LLMConfig(BaseModel):
    """Configuration for LLM gateway."""
    default_provider: str = "anthropic"

    # Provider mode: "direct" | "bedrock" | "hybrid"
    # - direct: Use provider APIs directly (default)
    # - bedrock: Route all calls through AWS Bedrock
    # - hybrid: Bedrock for Anthropic, direct API for OpenAI
    provider_mode: str = "direct"

    # AWS Bedrock configuration
    aws_region: Optional[str] = None
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None

    # Direct API tier models
    tier_models: dict[str, str] = Field(default_factory=lambda: {
        "fast": "claude-3-haiku-20240307",
        "standard": "claude-sonnet-4-20250514",
        "advanced": "claude-opus-4-20250514",
        "reasoning": "claude-opus-4-20250514"
    })

    # Bedrock tier models (Anthropic via Bedrock)
    bedrock_tier_models: dict[str, str] = Field(default_factory=lambda: {
        "fast": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
        "standard": "bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0",
        "advanced": "bedrock/anthropic.claude-opus-4-6-v1",
        "reasoning": "bedrock/anthropic.claude-opus-4-6-v1"
    })

    default_max_tokens: int = 8000
    default_temperature: float = 0.7
    timeout_seconds: int = 120
    max_retries: int = 3
    retry_delay_seconds: float = 1.0
    requests_per_minute: int = 60
    litellm_api_base: Optional[str] = None
    litellm_master_key: Optional[str] = None


class FunctionCall(BaseModel):
    """Function call details within a tool call."""
    name: str
    arguments: str  # JSON string


class ToolCallMessage(BaseModel):
    """A tool call in an assistant message (OpenAI format for LiteLLM)."""
    id: str
    type: str = "function"
    function: FunctionCall


class Message(BaseModel):
    """A message in the conversation."""
    role: str  # system, user, assistant, tool
    content: Optional[str] = None
    tool_call_id: Optional[str] = None  # Required for tool role messages
    tool_calls: Optional[list[ToolCallMessage]] = None  # For assistant messages

    @model_validator(mode='after')
    def validate_message(self) -> 'Message':
        if self.role == "tool" and self.tool_call_id is None:
            raise ValueError("tool_call_id is required for tool role messages")
        if self.tool_calls is not None and self.role != "assistant":
            raise ValueError("tool_calls can only be set for assistant messages")
        return self

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": self.role}

        # Content handling: can be None for assistant with tool_calls
        if self.content is not None:
            result["content"] = self.content
        elif self.role != "assistant" or self.tool_calls is None:
            # Non-assistant messages or assistant without tool_calls need content
            result["content"] = ""

        if self.tool_call_id is not None:
            result["tool_call_id"] = self.tool_call_id

        if self.tool_calls is not None:
            result["tool_calls"] = [tc.model_dump() for tc in self.tool_calls]

        return result


class ToolDefinition(BaseModel):
    """Tool definition for function calling."""
    name: str
    description: str
    parameters: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }


class ToolCall(BaseModel):
    """A tool call from the model response."""
    id: str
    name: str
    arguments: dict[str, Any]


class LLMResponse(BaseModel):
    """Response from LLM completion."""
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    model_used: str = ""
    finish_reason: str = ""
    latency_ms: int = 0


class StreamDelta(BaseModel):
    """A delta in a streaming response."""
    content: str = ""
    tool_call_id: Optional[str] = None
    tool_call_name: Optional[str] = None
    tool_call_args: Optional[str] = None
    finish_reason: Optional[str] = None


class LLMError(Exception):
    """Error from LLM gateway."""
    pass


class LLMGateway:
    """
    Unified LLM gateway with multi-provider support.

    Features:
    - Model tier routing
    - Automatic retries with backoff
    - Usage tracking and metering
    - Streaming support
    - Tool/function calling
    """

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig()

        if not LITELLM_AVAILABLE:
            logger.warning("LiteLLM not available, using mock responses")

        if LITELLM_AVAILABLE:
            if self.config.litellm_api_base:
                litellm.api_base = self.config.litellm_api_base

            # AWS Bedrock credentials (for hybrid/bedrock modes)
            if self.config.provider_mode in ("bedrock", "hybrid"):
                if self.config.aws_access_key_id:
                    os.environ.setdefault("AWS_ACCESS_KEY_ID", self.config.aws_access_key_id)
                if self.config.aws_secret_access_key:
                    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", self.config.aws_secret_access_key)
                if self.config.aws_region:
                    os.environ.setdefault("AWS_REGION_NAME", self.config.aws_region)
                else:
                    os.environ.setdefault("AWS_REGION_NAME", "us-east-1")

            # Direct API keys (always needed for hybrid, fallback)
            litellm.anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
            litellm.openai_key = os.environ.get("OPENAI_API_KEY")
            litellm.google_key = os.environ.get("GOOGLE_API_KEY")

        self._request_times: list[float] = []

    def _get_model_for_tier(self, tier: ModelTier) -> str:
        """Get model name for tier based on provider mode."""
        tier_value = tier.value
        base_model = self.config.tier_models.get(tier_value, self.config.tier_models["standard"])

        if self.config.provider_mode == "direct":
            return base_model

        if self.config.provider_mode == "bedrock":
            return self.config.bedrock_tier_models.get(
                tier_value, self.config.bedrock_tier_models["standard"]
            )

        # Hybrid mode: Anthropic via Bedrock, OpenAI direct
        if self.config.provider_mode == "hybrid":
            if "claude" in base_model.lower():
                return self.config.bedrock_tier_models.get(
                    tier_value, self.config.bedrock_tier_models["standard"]
                )
            return base_model  # OpenAI stays direct

        return base_model

    def _is_bedrock_error(self, error: Exception) -> bool:
        """Check if an error is a Bedrock-specific error that should trigger fallback."""
        error_str = str(error).lower()
        return any(x in error_str for x in [
            "bedrock", "throttlingexception", "accessdenied",
            "modelnotfound", "validationexception", "resourcenotfound"
        ])

    async def _check_rate_limit(self) -> None:
        """Check and enforce rate limiting."""
        now = time.time()
        minute_ago = now - 60

        self._request_times = [t for t in self._request_times if t > minute_ago]

        if len(self._request_times) >= self.config.requests_per_minute:
            wait_time = self._request_times[0] - minute_ago
            if wait_time > 0:
                logger.warning(f"Rate limit reached, waiting {wait_time:.1f}s")
                await asyncio.sleep(wait_time)

        self._request_times.append(now)

    async def complete(
        self,
        messages: list[Message],
        tier: ModelTier = ModelTier.STANDARD,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        tools: Optional[list[ToolDefinition]] = None,
        user_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> LLMResponse:
        """Complete a conversation."""
        await self._check_rate_limit()

        model = self._get_model_for_tier(tier)
        max_tokens = max_tokens or self.config.default_max_tokens
        temperature = temperature if temperature is not None else self.config.default_temperature

        messages_dict = [m.to_dict() for m in messages]
        tools_dict = [t.to_dict() for t in tools] if tools else None

        start_time = time.time()
        last_error: Optional[Exception] = None

        for attempt in range(self.config.max_retries):
            try:
                if LITELLM_AVAILABLE:
                    # Build kwargs conditionally - only include tools if we have them
                    # Anthropic rejects requests with tool_choice but no tools
                    completion_kwargs: dict[str, Any] = {
                        "model": model,
                        "messages": messages_dict,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "timeout": self.config.timeout_seconds,
                    }
                    if tools_dict:
                        completion_kwargs["tools"] = tools_dict

                    response = await acompletion(**completion_kwargs)
                else:
                    response = self._mock_response(messages_dict, tools_dict)

                latency_ms = int((time.time() - start_time) * 1000)
                result = self._parse_response(response, model, latency_ms)

                if user_id:
                    await self._track_usage(
                        user_id=user_id,
                        task_id=task_id,
                        session_id=session_id,
                        usage=result.usage,
                        model=model,
                        tier=tier.value
                    )

                return result

            except Exception as e:
                last_error = e
                logger.warning(f"LLM request failed (attempt {attempt + 1}): {e}")

                # Bedrock fallback: if using Bedrock and it fails, try direct API
                if self._is_bedrock_error(e) and self.config.provider_mode != "direct":
                    fallback_model = self.config.tier_models.get(
                        tier.value, self.config.tier_models["standard"]
                    )
                    if fallback_model != model:
                        logger.warning(f"Bedrock failed, falling back to direct API: {fallback_model}")
                        model = fallback_model
                        # Continue to next attempt with direct model
                        if attempt < self.config.max_retries - 1:
                            continue

                if attempt < self.config.max_retries - 1:
                    delay = self.config.retry_delay_seconds * (2 ** attempt)
                    await asyncio.sleep(delay)

        raise LLMError(f"LLM request failed after {self.config.max_retries} attempts") from last_error

    async def stream(
        self,
        messages: list[Message],
        tier: ModelTier = ModelTier.STANDARD,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        tools: Optional[list[ToolDefinition]] = None,
        user_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> AsyncIterator[StreamDelta]:
        """Stream a completion."""
        await self._check_rate_limit()

        model = self._get_model_for_tier(tier)
        max_tokens = max_tokens or self.config.default_max_tokens
        temperature = temperature if temperature is not None else self.config.default_temperature

        messages_dict = [m.to_dict() for m in messages]
        tools_dict = [t.to_dict() for t in tools] if tools else None

        total_tokens = 0

        try:
            if LITELLM_AVAILABLE:
                # Build kwargs conditionally - only include tools if we have them
                # Anthropic rejects requests with tool_choice but no tools
                completion_kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages_dict,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "timeout": self.config.timeout_seconds,
                    "stream": True,
                }
                if tools_dict:
                    completion_kwargs["tools"] = tools_dict

                response = await acompletion(**completion_kwargs)

                async for chunk in response:
                    delta = self._parse_stream_chunk(chunk)
                    if delta:
                        yield delta

                    if hasattr(chunk, 'usage') and chunk.usage:
                        total_tokens = chunk.usage.get('total_tokens', 0)
            else:
                for delta in self._mock_stream(messages_dict):
                    yield delta
                    await asyncio.sleep(0.05)

            if user_id:
                await self._track_usage(
                    user_id=user_id,
                    task_id=task_id,
                    session_id=session_id,
                    usage={'total_tokens': total_tokens},
                    model=model,
                    tier=tier.value
                )

        except Exception as e:
            logger.exception("Streaming failed")
            raise LLMError(f"Streaming failed: {e}") from e

    def _parse_response(
        self,
        response: Any,
        model: str,
        latency_ms: int
    ) -> LLMResponse:
        """Parse LiteLLM response into LLMResponse."""
        if not LITELLM_AVAILABLE:
            return response

        choice = response.choices[0]
        message = choice.message

        tool_calls = []
        if hasattr(message, 'tool_calls') and message.tool_calls:
            for tc in message.tool_calls:
                arguments = tc.function.arguments
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=arguments
                ))

        return LLMResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            usage={
                'prompt_tokens': response.usage.prompt_tokens if response.usage else 0,
                'completion_tokens': response.usage.completion_tokens if response.usage else 0,
                'total_tokens': response.usage.total_tokens if response.usage else 0
            },
            model_used=model,
            finish_reason=choice.finish_reason or "",
            latency_ms=latency_ms
        )

    def _parse_stream_chunk(self, chunk: Any) -> Optional[StreamDelta]:
        """Parse a streaming chunk into StreamDelta."""
        if not chunk.choices:
            return None

        choice = chunk.choices[0]
        delta = choice.delta

        stream_delta = StreamDelta()

        if hasattr(delta, 'content') and delta.content:
            stream_delta.content = delta.content

        if hasattr(delta, 'tool_calls') and delta.tool_calls:
            tc = delta.tool_calls[0]
            if hasattr(tc, 'id') and tc.id:
                stream_delta.tool_call_id = tc.id
            if hasattr(tc, 'function'):
                if hasattr(tc.function, 'name') and tc.function.name:
                    stream_delta.tool_call_name = tc.function.name
                if hasattr(tc.function, 'arguments') and tc.function.arguments:
                    stream_delta.tool_call_args = tc.function.arguments

        if choice.finish_reason:
            stream_delta.finish_reason = choice.finish_reason

        return stream_delta

    def _mock_response(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]]
    ) -> LLMResponse:
        """Generate mock response for testing."""
        last_message = messages[-1].get('content', '') if messages else ""

        return LLMResponse(
            content=f"Mock response to: {last_message[:100] if last_message else ''}",
            tool_calls=[],
            usage={
                'prompt_tokens': len(str(messages)) // 4,
                'completion_tokens': 50,
                'total_tokens': len(str(messages)) // 4 + 50
            },
            model_used="mock-model",
            finish_reason="stop",
            latency_ms=100
        )

    def _mock_stream(
        self,
        messages: list[dict[str, Any]]
    ) -> list[StreamDelta]:
        """Generate mock stream for testing."""
        last_message = messages[-1].get('content', '') if messages else ""
        response = f"Mock streaming response to: {last_message[:50] if last_message else ''}"

        deltas = []
        for word in response.split():
            deltas.append(StreamDelta(content=word + " "))

        deltas.append(StreamDelta(finish_reason="stop"))
        return deltas

    async def _track_usage(
        self,
        user_id: str,
        task_id: Optional[str],
        session_id: Optional[str],
        usage: dict[str, int],
        model: str,
        tier: str
    ) -> None:
        """Track LLM usage for metering.

        Raises:
            RuntimeError: If usage tracking fails (critical for billing accuracy)
        """
        try:
            await emit_usage_event(
                event_type="llm_tokens_used",
                user_id=user_id,
                session_id=session_id,
                task_id=task_id,
                quantity=usage.get('total_tokens', 0),
                unit="tokens",
                metadata={
                    'model': model,
                    'tier': tier,
                    'prompt_tokens': usage.get('prompt_tokens', 0),
                    'completion_tokens': usage.get('completion_tokens', 0)
                }
            )
        except Exception as e:
            # Usage tracking failures are critical for billing accuracy
            # Store error in database for analysis
            logger.error(
                f"Failed to track usage for user {user_id}, session {session_id}: {e}. "
                f"Tokens: {usage.get('total_tokens', 0)}, model: {model}"
            )
            # Store error asynchronously - don't block the response
            from apps.server.src.errors import handle_error, ErrorContext, GlockError
            try:
                import asyncio
                asyncio.create_task(handle_error(
                    e,
                    component="llm_gateway.usage_tracking",
                    context=ErrorContext(
                        user_id=user_id,
                        session_id=session_id,
                        task_id=task_id,
                        additional={
                            "model": model,
                            "tier": tier,
                            "tokens": usage.get('total_tokens', 0),
                        },
                    ),
                    reraise=False,
                ))
            except Exception:
                pass  # Don't fail if error storage fails
            raise GlockError(
                f"Usage tracking failed: {e}",
                original_error=e,
                severity="critical",
                context=ErrorContext(user_id=user_id, session_id=session_id),
            ) from e

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for text."""
        return len(text) // 4 + 1

    def get_context_window(self, tier: ModelTier) -> int:
        """Get context window size for model tier."""
        return 200000
