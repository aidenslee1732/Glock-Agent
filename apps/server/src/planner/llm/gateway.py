"""
LLM Gateway for Glock server.

Provides unified access to LLM providers through LiteLLM,
with support for:
- Multiple providers (Anthropic, OpenAI, Google)
- Model tier routing
- Usage tracking
- Error handling and retries
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List, Any, AsyncIterator, Union
import json

try:
    import litellm
    from litellm import acompletion, completion
    LITELLM_AVAILABLE = True
except ImportError:
    LITELLM_AVAILABLE = False

from ...metering.events import emit_usage_event


logger = logging.getLogger(__name__)


class ModelTier(Enum):
    """Model tiers for routing."""
    FAST = "fast"          # Quick, simple tasks
    STANDARD = "standard"  # Normal tasks
    ADVANCED = "advanced"  # Complex reasoning
    REASONING = "reasoning"  # Deep analysis


@dataclass
class LLMConfig:
    """Configuration for LLM gateway."""
    # Provider configuration
    default_provider: str = "anthropic"

    # Model mapping by tier
    tier_models: Dict[str, str] = field(default_factory=lambda: {
        "fast": "claude-3-haiku-20240307",
        "standard": "claude-sonnet-4-20250514",
        "advanced": "claude-opus-4-20250514",
        "reasoning": "claude-opus-4-20250514"
    })

    # Defaults
    default_max_tokens: int = 8000
    default_temperature: float = 0.7

    # Timeouts
    timeout_seconds: int = 120

    # Retries
    max_retries: int = 3
    retry_delay_seconds: float = 1.0

    # Rate limiting
    requests_per_minute: int = 60

    # LiteLLM settings
    litellm_api_base: Optional[str] = None
    litellm_master_key: Optional[str] = None


@dataclass
class Message:
    """A message in the conversation."""
    role: str  # system, user, assistant
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class ToolDefinition:
    """Tool definition for function calling."""
    name: str
    description: str
    parameters: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }


@dataclass
class ToolCall:
    """A tool call from the model."""
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    """Response from LLM completion."""
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)
    model_used: str = ""
    finish_reason: str = ""
    latency_ms: int = 0


@dataclass
class StreamDelta:
    """A delta in a streaming response."""
    content: str = ""
    tool_call_id: Optional[str] = None
    tool_call_name: Optional[str] = None
    tool_call_args: Optional[str] = None
    finish_reason: Optional[str] = None


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

        # Configure LiteLLM
        if LITELLM_AVAILABLE:
            if self.config.litellm_api_base:
                litellm.api_base = self.config.litellm_api_base

            # Set API keys from environment
            litellm.anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
            litellm.openai_key = os.environ.get("OPENAI_API_KEY")
            litellm.google_key = os.environ.get("GOOGLE_API_KEY")

        # Rate limiting
        self._request_times: List[float] = []

    def _get_model_for_tier(self, tier: ModelTier) -> str:
        """Get model name for tier."""
        return self.config.tier_models.get(tier.value, self.config.tier_models["standard"])

    async def _check_rate_limit(self) -> None:
        """Check and enforce rate limiting."""
        now = time.time()
        minute_ago = now - 60

        # Remove old request times
        self._request_times = [t for t in self._request_times if t > minute_ago]

        if len(self._request_times) >= self.config.requests_per_minute:
            # Wait for oldest request to expire
            wait_time = self._request_times[0] - minute_ago
            if wait_time > 0:
                logger.warning(f"Rate limit reached, waiting {wait_time:.1f}s")
                await asyncio.sleep(wait_time)

        self._request_times.append(now)

    async def complete(
        self,
        messages: List[Message],
        tier: ModelTier = ModelTier.STANDARD,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        tools: Optional[List[ToolDefinition]] = None,
        user_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> LLMResponse:
        """
        Complete a conversation.

        Args:
            messages: Conversation messages
            tier: Model tier to use
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature
            tools: Available tools for function calling
            user_id: For usage tracking
            task_id: For usage tracking
            session_id: For usage tracking

        Returns:
            LLMResponse with content and metadata
        """
        await self._check_rate_limit()

        model = self._get_model_for_tier(tier)
        max_tokens = max_tokens or self.config.default_max_tokens
        temperature = temperature if temperature is not None else self.config.default_temperature

        # Prepare messages
        messages_dict = [m.to_dict() for m in messages]

        # Prepare tools
        tools_dict = [t.to_dict() for t in tools] if tools else None

        start_time = time.time()
        last_error: Optional[Exception] = None

        for attempt in range(self.config.max_retries):
            try:
                if LITELLM_AVAILABLE:
                    response = await acompletion(
                        model=model,
                        messages=messages_dict,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        tools=tools_dict,
                        timeout=self.config.timeout_seconds
                    )
                else:
                    # Mock response for testing
                    response = self._mock_response(messages_dict, tools_dict)

                latency_ms = int((time.time() - start_time) * 1000)

                # Parse response
                result = self._parse_response(response, model, latency_ms)

                # Track usage
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
                logger.warning(
                    f"LLM request failed (attempt {attempt + 1}): {e}"
                )

                if attempt < self.config.max_retries - 1:
                    delay = self.config.retry_delay_seconds * (2 ** attempt)
                    await asyncio.sleep(delay)

        # All retries failed
        raise LLMError(f"LLM request failed after {self.config.max_retries} attempts") from last_error

    async def stream(
        self,
        messages: List[Message],
        tier: ModelTier = ModelTier.STANDARD,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        tools: Optional[List[ToolDefinition]] = None,
        user_id: Optional[str] = None,
        task_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> AsyncIterator[StreamDelta]:
        """
        Stream a completion.

        Yields StreamDelta objects as the response is generated.
        """
        await self._check_rate_limit()

        model = self._get_model_for_tier(tier)
        max_tokens = max_tokens or self.config.default_max_tokens
        temperature = temperature if temperature is not None else self.config.default_temperature

        # Prepare messages
        messages_dict = [m.to_dict() for m in messages]

        # Prepare tools
        tools_dict = [t.to_dict() for t in tools] if tools else None

        start_time = time.time()
        total_tokens = 0

        try:
            if LITELLM_AVAILABLE:
                response = await acompletion(
                    model=model,
                    messages=messages_dict,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    tools=tools_dict,
                    timeout=self.config.timeout_seconds,
                    stream=True
                )

                async for chunk in response:
                    delta = self._parse_stream_chunk(chunk)
                    if delta:
                        yield delta

                    # Track tokens from chunk
                    if hasattr(chunk, 'usage') and chunk.usage:
                        total_tokens = chunk.usage.get('total_tokens', 0)
            else:
                # Mock streaming for testing
                for delta in self._mock_stream(messages_dict):
                    yield delta
                    await asyncio.sleep(0.05)  # Simulate latency

            # Track usage
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
            return response  # Already LLMResponse from mock

        choice = response.choices[0]
        message = choice.message

        # Parse tool calls
        tool_calls = []
        if hasattr(message, 'tool_calls') and message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments
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
            if hasattr(tc, 'id'):
                stream_delta.tool_call_id = tc.id
            if hasattr(tc, 'function'):
                if hasattr(tc.function, 'name'):
                    stream_delta.tool_call_name = tc.function.name
                if hasattr(tc.function, 'arguments'):
                    stream_delta.tool_call_args = tc.function.arguments

        if choice.finish_reason:
            stream_delta.finish_reason = choice.finish_reason

        return stream_delta

    def _mock_response(
        self,
        messages: List[Dict],
        tools: Optional[List[Dict]]
    ) -> LLMResponse:
        """Generate mock response for testing."""
        last_message = messages[-1]['content'] if messages else ""

        return LLMResponse(
            content=f"Mock response to: {last_message[:100]}",
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
        messages: List[Dict]
    ) -> List[StreamDelta]:
        """Generate mock stream for testing."""
        last_message = messages[-1]['content'] if messages else ""
        response = f"Mock streaming response to: {last_message[:50]}"

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
        usage: Dict[str, int],
        model: str,
        tier: str
    ) -> None:
        """Track LLM usage for metering."""
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
            logger.warning(f"Failed to track usage: {e}")

    # Utility methods

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for text."""
        # Rough estimate: ~4 characters per token
        return len(text) // 4 + 1

    def get_context_window(self, tier: ModelTier) -> int:
        """Get context window size for model tier."""
        # Claude models generally support 200k tokens
        return 200000


class LLMError(Exception):
    """Error from LLM gateway."""
    pass
