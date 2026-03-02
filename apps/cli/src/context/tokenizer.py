"""Accurate Token Estimation for Model B.

Uses tiktoken for accurate token counting instead of the
crude len(text) // 4 heuristic.

This prevents:
- Context overflow from underestimation
- Underutilization from overestimation
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)


# Try to import tiktoken
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    logger.info("tiktoken not available, using heuristic token estimation")


class AccurateTokenizer:
    """Accurate token counting for LLM context management.

    Uses tiktoken when available, falls back to improved heuristics.

    Usage:
        tokenizer = AccurateTokenizer()
        count = tokenizer.count_tokens("Hello, world!")
        # Or for specific model
        tokenizer = AccurateTokenizer(model="gpt-4")
    """

    # Encoding names for different model families
    ENCODING_MAP = {
        # Claude uses cl100k_base (same as GPT-4)
        "claude": "cl100k_base",
        "claude-3": "cl100k_base",
        "claude-opus": "cl100k_base",
        "claude-sonnet": "cl100k_base",
        # GPT-4 and GPT-3.5
        "gpt-4": "cl100k_base",
        "gpt-3.5-turbo": "cl100k_base",
        # GPT-3
        "gpt-3": "p50k_base",
        "davinci": "p50k_base",
        "curie": "p50k_base",
        # Default
        "default": "cl100k_base",
    }

    # Heuristic ratios for different content types
    # (characters per token on average)
    CONTENT_RATIOS = {
        "code": 3.5,      # Code is denser
        "english": 4.0,   # English prose
        "json": 3.8,      # JSON structure
        "markdown": 4.2,  # Markdown with formatting
        "mixed": 3.7,     # Mixed content
    }

    def __init__(
        self,
        model: str = "claude",
        encoding: Optional[str] = None,
        fallback_ratio: float = 3.7,
    ):
        """Initialize tokenizer.

        Args:
            model: Model name for encoding selection
            encoding: Override encoding name
            fallback_ratio: Characters per token for heuristic
        """
        self._encoder = None
        self._fallback_ratio = fallback_ratio

        if TIKTOKEN_AVAILABLE:
            encoding_name = encoding or self._get_encoding_name(model)
            try:
                self._encoder = tiktoken.get_encoding(encoding_name)
                logger.debug(f"Using tiktoken encoding: {encoding_name}")
            except Exception as e:
                logger.warning(f"Failed to load tiktoken encoding: {e}")

    def _get_encoding_name(self, model: str) -> str:
        """Get tiktoken encoding name for a model."""
        model_lower = model.lower()

        for prefix, encoding in self.ENCODING_MAP.items():
            if prefix in model_lower:
                return encoding

        return self.ENCODING_MAP["default"]

    def count_tokens(self, text: str) -> int:
        """Count tokens in text.

        Args:
            text: Text to count tokens for

        Returns:
            Token count
        """
        if not text:
            return 0

        if self._encoder is not None:
            try:
                return len(self._encoder.encode(text))
            except Exception as e:
                logger.warning(f"tiktoken encoding failed: {e}")
                # Fall through to heuristic

        return self._heuristic_count(text)

    def _heuristic_count(self, text: str) -> int:
        """Estimate tokens using improved heuristics.

        Better than simple len // 4:
        - Adjusts for content type
        - Considers special characters
        - Accounts for whitespace
        """
        if not text:
            return 0

        # Detect content type
        ratio = self._detect_content_ratio(text)

        # Count characters
        char_count = len(text)

        # Adjust for special patterns
        adjustments = 0

        # Newlines often become separate tokens
        newlines = text.count("\n")
        adjustments += newlines * 0.5

        # Numbers often tokenize differently
        import re
        numbers = len(re.findall(r"\d+", text))
        adjustments += numbers * 0.3

        # Special characters
        specials = len(re.findall(r"[{}()\[\]<>\"']", text))
        adjustments += specials * 0.2

        # Calculate token estimate
        base_tokens = char_count / ratio
        total_tokens = base_tokens + adjustments

        return max(1, int(total_tokens))

    def _detect_content_ratio(self, text: str) -> float:
        """Detect content type and return appropriate ratio."""
        # Sample for efficiency
        sample = text[:1000]
        sample_lower = sample.lower()

        # Code indicators
        code_indicators = [
            "def ", "function ", "class ", "import ", "const ",
            "let ", "var ", "return ", "if (", "for (",
            "async ", "await ", "=>", "->",
        ]
        code_score = sum(1 for ind in code_indicators if ind in sample)

        # JSON indicators
        json_indicators = ['":', '",', '": ', '": "', "[{", "}]"]
        json_score = sum(1 for ind in json_indicators if ind in sample)

        # Markdown indicators
        md_indicators = ["##", "**", "- ", "```", "[](", "* "]
        md_score = sum(1 for ind in md_indicators if ind in sample)

        # Determine type
        if json_score >= 3:
            return self.CONTENT_RATIOS["json"]
        elif code_score >= 3:
            return self.CONTENT_RATIOS["code"]
        elif md_score >= 2:
            return self.CONTENT_RATIOS["markdown"]
        elif code_score + json_score >= 2:
            return self.CONTENT_RATIOS["mixed"]
        else:
            return self.CONTENT_RATIOS["english"]

    def count_messages_tokens(
        self,
        messages: list[dict],
        include_overhead: bool = True,
    ) -> int:
        """Count tokens in a list of messages.

        Args:
            messages: List of message dicts with 'role' and 'content'
            include_overhead: Add per-message overhead

        Returns:
            Total token count
        """
        total = 0

        for message in messages:
            content = message.get("content", "")
            if isinstance(content, str):
                total += self.count_tokens(content)
            elif isinstance(content, list):
                # Handle content arrays
                for item in content:
                    if isinstance(item, dict) and "text" in item:
                        total += self.count_tokens(item["text"])

            # Add overhead per message (role, formatting)
            if include_overhead:
                total += 4  # Approximate overhead per message

        # Add conversation overhead
        if include_overhead and messages:
            total += 3  # Conversation wrapper overhead

        return total

    def truncate_to_tokens(
        self,
        text: str,
        max_tokens: int,
        add_ellipsis: bool = True,
    ) -> str:
        """Truncate text to fit within token limit.

        Args:
            text: Text to truncate
            max_tokens: Maximum tokens
            add_ellipsis: Add "..." if truncated

        Returns:
            Truncated text
        """
        current_tokens = self.count_tokens(text)

        if current_tokens <= max_tokens:
            return text

        # Binary search for optimal truncation point
        left, right = 0, len(text)

        while left < right:
            mid = (left + right + 1) // 2
            truncated = text[:mid]
            if self.count_tokens(truncated) <= max_tokens - (3 if add_ellipsis else 0):
                left = mid
            else:
                right = mid - 1

        result = text[:left]

        if add_ellipsis and len(result) < len(text):
            result += "..."

        return result


# Global tokenizer instance
_default_tokenizer: Optional[AccurateTokenizer] = None


def get_tokenizer() -> AccurateTokenizer:
    """Get the global tokenizer instance."""
    global _default_tokenizer
    if _default_tokenizer is None:
        _default_tokenizer = AccurateTokenizer()
    return _default_tokenizer


def count_tokens(text: str) -> int:
    """Count tokens in text using the global tokenizer."""
    return get_tokenizer().count_tokens(text)


def estimate_tokens(text: str) -> int:
    """Alias for count_tokens for backward compatibility."""
    return count_tokens(text)


@lru_cache(maxsize=1000)
def count_tokens_cached(text: str) -> int:
    """Count tokens with caching for repeated calls.

    Note: Only use for immutable strings that won't change.
    """
    return get_tokenizer().count_tokens(text)
