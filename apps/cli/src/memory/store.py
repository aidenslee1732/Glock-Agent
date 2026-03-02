"""Persistent memory store using SQLite with FTS5.

Provides searchable, persistent storage for:
- Auto-extracted facts from conversations
- User preferences and settings
- Project-specific context
- Error solutions that worked

Uses SQLite with FTS5 for full-text search and optional embeddings
for semantic search.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .embeddings import EmbeddingManager

logger = logging.getLogger(__name__)


@dataclass
class Memory:
    """A stored memory item."""

    id: int
    key: str
    value: str
    category: str
    workspace: Optional[str]
    importance: float
    created_at: datetime
    updated_at: datetime
    use_count: int = 1
    embedding: Optional[bytes] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "key": self.key,
            "value": self.value,
            "category": self.category,
            "workspace": self.workspace,
            "importance": self.importance,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "use_count": self.use_count,
        }


class MemoryStore:
    """SQLite-based persistent memory store.

    Features:
    - Full-text search using FTS5
    - Workspace-scoped memories
    - Importance-weighted retrieval
    - Use count tracking
    - Optional embedding storage for semantic search
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        max_memories: int = 1000,
        embedding_manager: Optional["EmbeddingManager"] = None,
        auto_embed: bool = False,
    ):
        """Initialize memory store.

        Args:
            db_path: Path to SQLite database (defaults to ~/.glock/memory.db)
            max_memories: Maximum number of memories to keep
            embedding_manager: Optional embedding manager for semantic search
            auto_embed: Whether to automatically generate embeddings on add
        """
        if db_path is None:
            db_path = Path.home() / ".glock" / "memory.db"

        self.db_path = db_path
        self.max_memories = max_memories
        self._embedding_manager = embedding_manager
        self._auto_embed = auto_embed

        # Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self._init_db()

    def set_embedding_manager(self, manager: "EmbeddingManager") -> None:
        """Set the embedding manager.

        Args:
            manager: EmbeddingManager instance
        """
        self._embedding_manager = manager

    def _init_db(self) -> None:
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    workspace TEXT,
                    importance REAL DEFAULT 0.5,
                    use_count INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    embedding BLOB
                )
            """)

            # Create FTS5 virtual table for full-text search
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    key,
                    value,
                    category,
                    content='memories',
                    content_rowid='id'
                )
            """)

            # Create triggers to keep FTS in sync
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, key, value, category)
                    VALUES (new.id, new.key, new.value, new.category);
                END
            """)

            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, key, value, category)
                    VALUES ('delete', old.id, old.key, old.value, old.category);
                END
            """)

            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, key, value, category)
                    VALUES ('delete', old.id, old.key, old.value, old.category);
                    INSERT INTO memories_fts(rowid, key, value, category)
                    VALUES (new.id, new.key, new.value, new.category);
                END
            """)

            # Create indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_workspace ON memories(workspace)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC)")

            conn.commit()

    def add(
        self,
        key: str,
        value: str,
        category: str = "general",
        workspace: Optional[str] = None,
        importance: float = 0.5,
        embedding: Optional[bytes] = None,
    ) -> int:
        """Add or update a memory.

        Args:
            key: Unique identifier for the memory
            value: Memory content
            category: Category (general, error_solution, user_preference, etc.)
            workspace: Optional workspace path to scope memory
            importance: Importance score (0.0 to 1.0)
            embedding: Optional pre-computed embedding bytes

        Returns:
            Memory ID
        """
        # Auto-generate embedding if enabled and not provided
        if embedding is None and self._auto_embed and self._embedding_manager:
            try:
                from .embeddings import serialize_embedding
                text_to_embed = f"{key}: {value}"
                embedding_vector = self._embedding_manager.embed(text_to_embed)
                embedding = serialize_embedding(embedding_vector)
            except Exception as e:
                logger.warning(f"Failed to generate embedding: {e}")

        with sqlite3.connect(self.db_path) as conn:
            # Try to update existing
            if embedding:
                cursor = conn.execute(
                    """
                    UPDATE memories
                    SET value = ?,
                        category = ?,
                        importance = ?,
                        embedding = ?,
                        use_count = use_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE key = ?
                    """,
                    (value, category, importance, embedding, key),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE memories
                    SET value = ?,
                        category = ?,
                        importance = ?,
                        use_count = use_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE key = ?
                    """,
                    (value, category, importance, key),
                )

            if cursor.rowcount == 0:
                # Insert new
                cursor = conn.execute(
                    """
                    INSERT INTO memories (key, value, category, workspace, importance, embedding)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (key, value, category, workspace, importance, embedding),
                )

            conn.commit()

            # Get the ID
            cursor = conn.execute("SELECT id FROM memories WHERE key = ?", (key,))
            row = cursor.fetchone()

            # Enforce max memories limit
            self._enforce_limit(conn)

            return row[0] if row else 0

    def get(self, key: str) -> Optional[Memory]:
        """Get a memory by key.

        Args:
            key: Memory key

        Returns:
            Memory object or None
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT * FROM memories WHERE key = ?
                """,
                (key,),
            )
            row = cursor.fetchone()

            if row:
                # Update use count
                conn.execute(
                    """
                    UPDATE memories
                    SET use_count = use_count + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE key = ?
                    """,
                    (key,),
                )
                conn.commit()

                return self._row_to_memory(row)

            return None

    def search(
        self,
        query: str,
        limit: int = 10,
        category: Optional[str] = None,
        workspace: Optional[str] = None,
    ) -> list[Memory]:
        """Full-text search memories.

        Args:
            query: Search query
            limit: Maximum results to return
            category: Optional category filter
            workspace: Optional workspace filter

        Returns:
            List of matching memories sorted by relevance
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Build query with optional filters
            sql = """
                SELECT m.*, bm25(memories_fts) as rank
                FROM memories_fts fts
                JOIN memories m ON fts.rowid = m.id
                WHERE memories_fts MATCH ?
            """
            params: list[Any] = [query]

            if category:
                sql += " AND m.category = ?"
                params.append(category)

            if workspace:
                sql += " AND (m.workspace = ? OR m.workspace IS NULL)"
                params.append(workspace)

            sql += " ORDER BY rank, m.importance DESC, m.use_count DESC LIMIT ?"
            params.append(limit)

            try:
                cursor = conn.execute(sql, params)
                rows = cursor.fetchall()
                return [self._row_to_memory(row) for row in rows]
            except sqlite3.OperationalError:
                # FTS query syntax error - fall back to LIKE search
                return self._fallback_search(conn, query, limit, category, workspace)

    def _fallback_search(
        self,
        conn: sqlite3.Connection,
        query: str,
        limit: int,
        category: Optional[str],
        workspace: Optional[str],
    ) -> list[Memory]:
        """Fallback LIKE-based search when FTS fails."""
        sql = """
            SELECT * FROM memories
            WHERE (key LIKE ? OR value LIKE ?)
        """
        params: list[Any] = [f"%{query}%", f"%{query}%"]

        if category:
            sql += " AND category = ?"
            params.append(category)

        if workspace:
            sql += " AND (workspace = ? OR workspace IS NULL)"
            params.append(workspace)

        sql += " ORDER BY importance DESC, use_count DESC LIMIT ?"
        params.append(limit)

        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
        return [self._row_to_memory(row) for row in rows]

    def list_by_category(
        self,
        category: str,
        workspace: Optional[str] = None,
        limit: int = 50,
    ) -> list[Memory]:
        """List memories by category.

        Args:
            category: Category to filter by
            workspace: Optional workspace filter
            limit: Maximum results

        Returns:
            List of memories
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            if workspace:
                cursor = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE category = ? AND (workspace = ? OR workspace IS NULL)
                    ORDER BY importance DESC, use_count DESC
                    LIMIT ?
                    """,
                    (category, workspace, limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE category = ?
                    ORDER BY importance DESC, use_count DESC
                    LIMIT ?
                    """,
                    (category, limit),
                )

            rows = cursor.fetchall()
            return [self._row_to_memory(row) for row in rows]

    def remove(self, key: str) -> bool:
        """Remove a memory.

        Args:
            key: Memory key

        Returns:
            True if removed, False if not found
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM memories WHERE key = ?", (key,))
            conn.commit()
            return cursor.rowcount > 0

    def update_importance(self, key: str, importance: float) -> bool:
        """Update importance score for a memory.

        Args:
            key: Memory key
            importance: New importance score (0.0 to 1.0)

        Returns:
            True if updated, False if not found
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE memories
                SET importance = ?, updated_at = CURRENT_TIMESTAMP
                WHERE key = ?
                """,
                (importance, key),
            )
            conn.commit()
            return cursor.rowcount > 0

    def store_embedding(self, key: str, embedding: bytes) -> bool:
        """Store embedding vector for a memory.

        Args:
            key: Memory key
            embedding: Serialized embedding vector

        Returns:
            True if updated, False if not found
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE memories
                SET embedding = ?, updated_at = CURRENT_TIMESTAMP
                WHERE key = ?
                """,
                (embedding, key),
            )
            conn.commit()
            return cursor.rowcount > 0

    def semantic_search(
        self,
        query: str,
        limit: int = 10,
        workspace: Optional[str] = None,
        threshold: float = 0.5,
    ) -> list[tuple[Memory, float]]:
        """Semantic search using embeddings.

        Computes cosine similarity between query and stored embeddings.
        Requires embedding_manager to be set.

        Args:
            query: Query text to search for
            limit: Maximum results
            workspace: Optional workspace filter
            threshold: Minimum similarity score (0-1)

        Returns:
            List of (Memory, score) tuples sorted by similarity
        """
        if not self._embedding_manager:
            logger.warning("Semantic search requires embedding manager - falling back to FTS")
            fts_results = self.search(query, limit, workspace=workspace)
            return [(m, 1.0) for m in fts_results]

        try:
            from .embeddings import deserialize_embedding, cosine_similarity

            # Generate query embedding
            query_embedding = self._embedding_manager.embed(query)

            # Get all memories with embeddings
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row

                if workspace:
                    cursor = conn.execute(
                        """
                        SELECT * FROM memories
                        WHERE embedding IS NOT NULL
                        AND (workspace = ? OR workspace IS NULL)
                        """,
                        (workspace,),
                    )
                else:
                    cursor = conn.execute(
                        """
                        SELECT * FROM memories
                        WHERE embedding IS NOT NULL
                        """
                    )

                rows = cursor.fetchall()

            # Compute similarities
            scored_memories: list[tuple[Memory, float]] = []
            for row in rows:
                try:
                    stored_embedding = deserialize_embedding(row["embedding"])
                    # Convert cosine similarity from [-1, 1] to [0, 1]
                    similarity = (cosine_similarity(query_embedding, stored_embedding) + 1) / 2

                    if similarity >= threshold:
                        memory = self._row_to_memory(row)
                        scored_memories.append((memory, similarity))
                except Exception as e:
                    logger.debug(f"Failed to compute similarity for memory {row['id']}: {e}")
                    continue

            # Sort by similarity descending
            scored_memories.sort(key=lambda x: x[1], reverse=True)

            return scored_memories[:limit]

        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            # Fall back to FTS
            fts_results = self.search(query, limit, workspace=workspace)
            return [(m, 1.0) for m in fts_results]

    def hybrid_search(
        self,
        query: str,
        limit: int = 10,
        workspace: Optional[str] = None,
        semantic_weight: float = 0.5,
    ) -> list[tuple[Memory, float]]:
        """Combined FTS and semantic search.

        Args:
            query: Search query
            limit: Maximum results
            workspace: Optional workspace filter
            semantic_weight: Weight for semantic scores vs FTS (0-1)

        Returns:
            List of (Memory, combined_score) tuples
        """
        fts_weight = 1.0 - semantic_weight

        # Get FTS results
        fts_results = self.search(query, limit * 2, workspace=workspace)
        fts_scores: dict[int, float] = {}

        for i, memory in enumerate(fts_results):
            # Higher rank for earlier results
            fts_scores[memory.id] = 1.0 - (i / len(fts_results)) if fts_results else 0

        # Get semantic results
        semantic_results = self.semantic_search(query, limit * 2, workspace=workspace)
        semantic_scores: dict[int, float] = {}

        for memory, score in semantic_results:
            semantic_scores[memory.id] = score

        # Combine results
        all_memory_ids = set(fts_scores.keys()) | set(semantic_scores.keys())
        combined: list[tuple[Memory, float]] = []

        # Get all memories
        memories_by_id: dict[int, Memory] = {}
        for memory in fts_results:
            memories_by_id[memory.id] = memory
        for memory, _ in semantic_results:
            memories_by_id[memory.id] = memory

        for mid in all_memory_ids:
            fts_score = fts_scores.get(mid, 0)
            sem_score = semantic_scores.get(mid, 0)
            combined_score = (fts_weight * fts_score) + (semantic_weight * sem_score)

            if mid in memories_by_id:
                combined.append((memories_by_id[mid], combined_score))

        # Sort by combined score
        combined.sort(key=lambda x: x[1], reverse=True)

        return combined[:limit]

    def generate_embeddings_for_all(self, batch_size: int = 32) -> int:
        """Generate embeddings for all memories that don't have them.

        Args:
            batch_size: Number of memories to process at once

        Returns:
            Number of embeddings generated
        """
        if not self._embedding_manager:
            logger.error("Embedding manager not set")
            return 0

        from .embeddings import serialize_embedding

        count = 0

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Get memories without embeddings
            cursor = conn.execute(
                """
                SELECT id, key, value FROM memories
                WHERE embedding IS NULL
                """
            )
            rows = cursor.fetchall()

            # Process in batches
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                texts = [f"{row['key']}: {row['value']}" for row in batch]

                try:
                    embeddings = self._embedding_manager.embed_batch(texts)

                    for row, embedding in zip(batch, embeddings):
                        conn.execute(
                            """
                            UPDATE memories
                            SET embedding = ?
                            WHERE id = ?
                            """,
                            (serialize_embedding(embedding), row["id"]),
                        )
                        count += 1

                    conn.commit()
                    logger.debug(f"Generated embeddings for batch {i // batch_size + 1}")

                except Exception as e:
                    logger.error(f"Failed to generate embeddings for batch: {e}")

        logger.info(f"Generated {count} embeddings")
        return count

    def get_all_for_context(
        self,
        workspace: Optional[str] = None,
        max_tokens: int = 2000,
    ) -> str:
        """Get relevant memories formatted for context.

        Args:
            workspace: Optional workspace filter
            max_tokens: Approximate token limit

        Returns:
            Formatted string of memories for context injection
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            if workspace:
                cursor = conn.execute(
                    """
                    SELECT * FROM memories
                    WHERE workspace = ? OR workspace IS NULL
                    ORDER BY importance DESC, use_count DESC, updated_at DESC
                    LIMIT 50
                    """,
                    (workspace,),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM memories
                    ORDER BY importance DESC, use_count DESC, updated_at DESC
                    LIMIT 50
                    """,
                )

            rows = cursor.fetchall()

            lines: list[str] = []
            total_chars = 0
            char_limit = max_tokens * 4  # Rough token to char conversion

            for row in rows:
                line = f"- {row['key']}: {row['value']}"
                if total_chars + len(line) > char_limit:
                    break
                lines.append(line)
                total_chars += len(line)

            if not lines:
                return ""

            return "## Remembered Context\n" + "\n".join(lines)

    def _row_to_memory(self, row: sqlite3.Row) -> Memory:
        """Convert database row to Memory object."""
        return Memory(
            id=row["id"],
            key=row["key"],
            value=row["value"],
            category=row["category"],
            workspace=row["workspace"],
            importance=row["importance"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            use_count=row["use_count"],
            embedding=row["embedding"],
        )

    def _enforce_limit(self, conn: sqlite3.Connection) -> None:
        """Remove oldest/least important memories if over limit."""
        cursor = conn.execute("SELECT COUNT(*) FROM memories")
        count = cursor.fetchone()[0]

        if count > self.max_memories:
            # Delete lowest scored memories
            to_delete = count - self.max_memories
            conn.execute(
                """
                DELETE FROM memories
                WHERE id IN (
                    SELECT id FROM memories
                    ORDER BY importance ASC, use_count ASC, updated_at ASC
                    LIMIT ?
                )
                """,
                (to_delete,),
            )
            conn.commit()
            logger.debug(f"Pruned {to_delete} low-importance memories")

    def stats(self) -> dict[str, Any]:
        """Get memory store statistics."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM memories")
            total = cursor.fetchone()[0]

            cursor = conn.execute(
                """
                SELECT category, COUNT(*) as count
                FROM memories
                GROUP BY category
                """
            )
            by_category = {row[0]: row[1] for row in cursor.fetchall()}

            cursor = conn.execute(
                """
                SELECT workspace, COUNT(*) as count
                FROM memories
                WHERE workspace IS NOT NULL
                GROUP BY workspace
                """
            )
            by_workspace = {row[0]: row[1] for row in cursor.fetchall()}

            return {
                "total": total,
                "by_category": by_category,
                "by_workspace": by_workspace,
                "db_path": str(self.db_path),
            }

    def clear(self, workspace: Optional[str] = None) -> int:
        """Clear memories.

        Args:
            workspace: If provided, only clear memories for this workspace

        Returns:
            Number of memories deleted
        """
        with sqlite3.connect(self.db_path) as conn:
            if workspace:
                cursor = conn.execute(
                    "DELETE FROM memories WHERE workspace = ?",
                    (workspace,),
                )
            else:
                cursor = conn.execute("DELETE FROM memories")

            conn.commit()
            return cursor.rowcount
