"""Gateway protocol utilities."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from packages.shared_protocol.types import (
    MessageEnvelope,
    MessageType,
)

logger = logging.getLogger(__name__)

# Provider terms to sanitize (case-insensitive)
PROVIDER_DENYLIST = [
    "claude", "anthropic", "openai", "gemini", "gpt-",
    "opus", "sonnet", "haiku", "api.anthropic", "api.openai",
    "generativelanguage", "litellm"
]


class GatewayProtocol:
    """Protocol utilities for gateway message handling."""

    @staticmethod
    def parse_message(data: str | bytes) -> MessageEnvelope:
        """Parse incoming WebSocket message."""
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        parsed = json.loads(data)
        return MessageEnvelope.from_dict(parsed)

    @staticmethod
    def serialize_message(msg: MessageEnvelope) -> str:
        """Serialize message for WebSocket."""
        return json.dumps(msg.to_dict())

    @staticmethod
    def create_error(
        session_id: str,
        error_code: str,
        message: str,
        seq: int = 0,
        task_id: Optional[str] = None,
    ) -> MessageEnvelope:
        """Create an error message."""
        return MessageEnvelope.create(
            msg_type=MessageType.SESSION_ERROR,
            session_id=session_id,
            payload={
                "error_code": error_code,
                "message": message,
            },
            seq=seq,
            task_id=task_id,
        )

    @staticmethod
    def create_warning(
        session_id: str,
        warning_code: str,
        message: str,
        seq: int = 0,
    ) -> MessageEnvelope:
        """Create a warning message."""
        return MessageEnvelope.create(
            msg_type=MessageType.WARNING,
            session_id=session_id,
            payload={
                "warning": warning_code,
                "message": message,
            },
            seq=seq,
        )


class ClientSanitizer:
    """Sanitizes all client-bound content to prevent provider leakage."""

    @staticmethod
    def sanitize_message(msg: MessageEnvelope) -> MessageEnvelope:
        """Sanitize message before sending to client."""
        # Scrub payload
        msg.payload = ClientSanitizer._scrub_payload(msg.payload)
        return msg

    @staticmethod
    def sanitize_text(text: str) -> str:
        """Scrub provider references from text."""
        for term in PROVIDER_DENYLIST:
            pattern = rf'\b{re.escape(term)}\b'
            text = re.sub(pattern, 'Glock', text, flags=re.IGNORECASE)
        return text

    @staticmethod
    def sanitize_error(error: Exception) -> dict[str, Any]:
        """Convert provider error to generic client error."""
        error_str = str(error).lower()

        # Map to generic error codes
        if "rate" in error_str and "limit" in error_str:
            return {
                "code": "rate_limit",
                "message": "Request rate limit exceeded. Please try again later.",
            }
        elif "timeout" in error_str:
            return {
                "code": "timeout",
                "message": "Request timed out. Please try again.",
            }
        elif "connection" in error_str:
            return {
                "code": "upstream_unavailable",
                "message": "Service temporarily unavailable. Please try again.",
            }
        elif "auth" in error_str or "key" in error_str:
            return {
                "code": "internal_error",
                "message": "Internal configuration error.",
            }
        else:
            return {
                "code": "internal_error",
                "message": "An unexpected error occurred.",
            }

    @staticmethod
    def _scrub_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Remove provider-specific fields from payload."""
        # Remove provider fields
        provider_fields = ["provider_used", "model_name", "model_id", "provider"]
        for field in provider_fields:
            payload.pop(field, None)

        # Sanitize error messages
        if "error" in payload:
            if isinstance(payload["error"], str):
                payload["error"] = ClientSanitizer.sanitize_text(payload["error"])
            elif isinstance(payload["error"], dict):
                if "message" in payload["error"]:
                    payload["error"]["message"] = ClientSanitizer.sanitize_text(
                        payload["error"]["message"]
                    )

        # Sanitize content/text fields
        for field in ["content", "text", "message", "summary"]:
            if field in payload and isinstance(payload[field], str):
                payload[field] = ClientSanitizer.sanitize_text(payload[field])

        return payload


# Error mappings for provider errors
ERROR_MAPPINGS = {
    "anthropic.RateLimitError": "rate_limit",
    "anthropic.APIConnectionError": "upstream_unavailable",
    "anthropic.AuthenticationError": "internal_error",
    "openai.RateLimitError": "rate_limit",
    "openai.APIConnectionError": "upstream_unavailable",
    "google.api_core.exceptions.ResourceExhausted": "rate_limit",
}

GENERIC_ERROR_MESSAGES = {
    "rate_limit": "Request rate limit exceeded. Please try again later.",
    "timeout": "Request timed out. Please try again.",
    "upstream_unavailable": "Service temporarily unavailable. Please try again.",
    "internal_error": "An unexpected error occurred.",
    "session_limit_exceeded": "Maximum session limit reached.",
    "task_limit_exceeded": "Maximum concurrent task limit reached.",
    "validation_error": "Invalid request.",
}
