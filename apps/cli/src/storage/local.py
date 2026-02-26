"""Local storage for CLI state and session history."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class LocalStorage:
    """Local storage for Glock CLI.

    Stores:
    - Session history
    - User preferences
    - Auth tokens
    - Plan public keys
    """

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or self._get_default_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self._sessions_file = self.base_dir / "sessions.json"
        self._config_file = self.base_dir / "config.json"
        self._keys_file = self.base_dir / "keys.json"

    @staticmethod
    def _get_default_dir() -> Path:
        """Get default storage directory."""
        import os
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return Path(xdg_data) / "glock"

        return Path.home() / ".glock"

    async def get_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent sessions.

        Args:
            limit: Maximum number of sessions to return

        Returns:
            List of session records
        """
        if not self._sessions_file.exists():
            return []

        try:
            data = json.loads(self._sessions_file.read_text())
            sessions = data.get("sessions", [])
            return sessions[:limit]
        except Exception as e:
            logger.warning(f"Failed to load sessions: {e}")
            return []

    async def save_session(self, session: dict[str, Any]) -> None:
        """Save session to history.

        Args:
            session: Session record to save
        """
        sessions = await self.get_sessions(limit=100)

        # Update or append
        session_id = session.get("session_id")
        updated = False
        for i, s in enumerate(sessions):
            if s.get("session_id") == session_id:
                sessions[i] = session
                updated = True
                break

        if not updated:
            sessions.insert(0, session)

        # Keep only recent
        sessions = sessions[:100]

        try:
            self._sessions_file.write_text(json.dumps({"sessions": sessions}, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save sessions: {e}")

    async def get_config(self) -> dict[str, Any]:
        """Get user configuration.

        Returns:
            Configuration dict
        """
        if not self._config_file.exists():
            return {}

        try:
            return json.loads(self._config_file.read_text())
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")
            return {}

    async def save_config(self, config: dict[str, Any]) -> None:
        """Save user configuration.

        Args:
            config: Configuration dict
        """
        try:
            self._config_file.write_text(json.dumps(config, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save config: {e}")

    async def get_auth_token(self) -> Optional[str]:
        """Get stored auth token.

        Returns:
            Auth token or None
        """
        config = await self.get_config()
        return config.get("auth_token")

    async def save_auth_token(self, token: str) -> None:
        """Save auth token.

        Args:
            token: Auth token
        """
        config = await self.get_config()
        config["auth_token"] = token
        await self.save_config(config)

    async def get_public_keys(self) -> dict[str, str]:
        """Get plan signing public keys.

        Returns:
            Dict of kid → base64 public key
        """
        if not self._keys_file.exists():
            return {}

        try:
            return json.loads(self._keys_file.read_text())
        except Exception as e:
            logger.warning(f"Failed to load keys: {e}")
            return {}

    async def save_public_keys(self, keys: dict[str, str]) -> None:
        """Save plan signing public keys.

        Args:
            keys: Dict of kid → base64 public key
        """
        try:
            self._keys_file.write_text(json.dumps(keys, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save keys: {e}")
