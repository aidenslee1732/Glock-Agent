"""Client-side replay buffer for tracking sent messages."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class BufferedMessage:
    """A message stored in the replay buffer."""
    seq: int
    msg_type: str
    payload: dict[str, Any]
    task_id: Optional[str] = None


class ClientReplayBuffer:
    """Client-side buffer for tracking sent messages.

    Used to:
    - Track messages that haven't been acked
    - Provide messages for replay on reconnect
    """

    def __init__(self, max_size: int = 100):
        self.max_size = max_size
        self._buffer: deque[BufferedMessage] = deque(maxlen=max_size)
        self._last_acked_seq = 0

    def append(
        self,
        seq: int,
        msg_type: str,
        payload: dict[str, Any],
        task_id: Optional[str] = None,
    ) -> None:
        """Append a message to the buffer.

        Args:
            seq: Sequence number
            msg_type: Message type
            payload: Message payload
            task_id: Optional task ID
        """
        self._buffer.append(BufferedMessage(
            seq=seq,
            msg_type=msg_type,
            payload=payload,
            task_id=task_id,
        ))

    def ack(self, seq: int) -> None:
        """Acknowledge messages up to sequence number.

        Args:
            seq: Sequence number to ack
        """
        self._last_acked_seq = seq

        # Remove acked messages
        while self._buffer and self._buffer[0].seq <= seq:
            self._buffer.popleft()

    def get_unacked(self) -> list[BufferedMessage]:
        """Get unacked messages.

        Returns:
            List of unacked messages
        """
        return [
            msg for msg in self._buffer
            if msg.seq > self._last_acked_seq
        ]

    def get_since(self, seq: int) -> list[BufferedMessage]:
        """Get messages since sequence number.

        Args:
            seq: Sequence number

        Returns:
            List of messages after seq
        """
        return [msg for msg in self._buffer if msg.seq > seq]

    @property
    def last_seq(self) -> int:
        """Get last sequence number in buffer."""
        if self._buffer:
            return self._buffer[-1].seq
        return 0

    @property
    def last_acked_seq(self) -> int:
        """Get last acked sequence number."""
        return self._last_acked_seq

    def clear(self) -> None:
        """Clear the buffer."""
        self._buffer.clear()
        self._last_acked_seq = 0
