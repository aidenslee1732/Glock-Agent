"""Vector embeddings for semantic memory search.

Provides:
- Multiple embedding backends (local sentence-transformers, OpenAI, Voyage)
- Vector similarity computation using numpy
- Optional sqlite-vss integration for faster search
- Embedding caching to reduce API calls
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union
import sqlite3

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingConfig:
    """Configuration for embedding generation.

    Attributes:
        provider: "local", "openai", or "voyage"
        model_name: Model identifier
        api_key: API key for remote providers
        dimension: Embedding dimension
        batch_size: Batch size for embedding
        cache_enabled: Whether to cache embeddings
    """
    provider: str = "local"
    model_name: str = "all-MiniLM-L6-v2"
    api_key: Optional[str] = None
    dimension: int = 384
    batch_size: int = 32
    cache_enabled: bool = True


def serialize_embedding(embedding: list[float]) -> bytes:
    """Serialize embedding to bytes for storage.

    Args:
        embedding: List of float values

    Returns:
        Packed bytes
    """
    return struct.pack(f'{len(embedding)}f', *embedding)


def deserialize_embedding(data: bytes) -> list[float]:
    """Deserialize embedding from bytes.

    Args:
        data: Packed bytes

    Returns:
        List of float values
    """
    count = len(data) // 4  # 4 bytes per float
    return list(struct.unpack(f'{count}f', data))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Args:
        a: First vector
        b: Second vector

    Returns:
        Cosine similarity score (-1 to 1)
    """
    # Use numpy if available, otherwise pure Python
    try:
        import numpy as np
        a_np = np.array(a)
        b_np = np.array(b)
        return float(np.dot(a_np, b_np) / (np.linalg.norm(a_np) * np.linalg.norm(b_np)))
    except ImportError:
        # Pure Python fallback
        dot_product = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot_product / (norm_a * norm_b)


class EmbeddingProvider(ABC):
    """Abstract base class for embedding providers."""

    @abstractmethod
    def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a single text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        pass

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Args:
            texts: Texts to embed

        Returns:
            List of embedding vectors
        """
        pass

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Get embedding dimension."""
        pass


class LocalEmbeddingProvider(EmbeddingProvider):
    """Local embedding using sentence-transformers.

    Falls back to a simple hash-based embedding if sentence-transformers
    is not installed.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        """Initialize local embedding provider.

        Args:
            model_name: Sentence-transformers model name
        """
        self._model_name = model_name
        self._model = None
        self._dimension = 384  # Default for MiniLM

        # Try to load model
        self._load_model()

    def _load_model(self) -> None:
        """Load the sentence-transformers model."""
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name)
            self._dimension = self._model.get_sentence_embedding_dimension()
            logger.info(f"Loaded embedding model: {self._model_name}")
        except ImportError:
            logger.warning(
                "sentence-transformers not installed. "
                "Install with: pip install sentence-transformers"
            )
            self._model = None
        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            self._model = None

    def embed_text(self, text: str) -> list[float]:
        """Generate embedding for text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        if self._model is not None:
            import numpy as np
            embedding = self._model.encode(text, convert_to_numpy=True)
            return embedding.tolist()
        else:
            # Fallback: hash-based pseudo-embedding
            return self._hash_embedding(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Args:
            texts: Texts to embed

        Returns:
            List of embedding vectors
        """
        if self._model is not None:
            import numpy as np
            embeddings = self._model.encode(texts, convert_to_numpy=True)
            return embeddings.tolist()
        else:
            return [self._hash_embedding(text) for text in texts]

    def _hash_embedding(self, text: str) -> list[float]:
        """Create a pseudo-embedding from text hash.

        This is NOT a real semantic embedding and should only be used
        as a fallback when sentence-transformers is unavailable.

        Args:
            text: Text to hash

        Returns:
            Pseudo-embedding vector
        """
        # Create deterministic hash-based values
        hash_bytes = hashlib.sha256(text.encode()).digest() * 12  # 384 bytes
        embedding = []
        for i in range(self._dimension):
            # Convert byte to float in [-1, 1]
            val = (hash_bytes[i] / 127.5) - 1.0
            embedding.append(val)

        # Normalize
        norm = sum(x * x for x in embedding) ** 0.5
        return [x / norm for x in embedding]

    @property
    def dimension(self) -> int:
        """Get embedding dimension."""
        return self._dimension


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Embedding using OpenAI's API."""

    def __init__(
        self,
        api_key: str,
        model_name: str = "text-embedding-3-small",
    ):
        """Initialize OpenAI embedding provider.

        Args:
            api_key: OpenAI API key
            model_name: Model identifier
        """
        self._api_key = api_key
        self._model_name = model_name
        self._dimension = 1536
        if "small" in model_name:
            self._dimension = 1536
        elif "large" in model_name:
            self._dimension = 3072

    def embed_text(self, text: str) -> list[float]:
        """Generate embedding for text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Args:
            texts: Texts to embed

        Returns:
            List of embedding vectors
        """
        try:
            import httpx
        except ImportError:
            logger.error("httpx not installed for OpenAI embeddings")
            raise ImportError("httpx required for OpenAI embeddings")

        response = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model_name,
                "input": texts,
            },
            timeout=30.0,
        )

        response.raise_for_status()
        data = response.json()

        # Sort by index to maintain order
        embeddings_data = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in embeddings_data]

    @property
    def dimension(self) -> int:
        """Get embedding dimension."""
        return self._dimension


class VoyageEmbeddingProvider(EmbeddingProvider):
    """Embedding using Voyage AI's API."""

    def __init__(
        self,
        api_key: str,
        model_name: str = "voyage-2",
    ):
        """Initialize Voyage embedding provider.

        Args:
            api_key: Voyage API key
            model_name: Model identifier
        """
        self._api_key = api_key
        self._model_name = model_name
        self._dimension = 1024

    def embed_text(self, text: str) -> list[float]:
        """Generate embedding for text.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Args:
            texts: Texts to embed

        Returns:
            List of embedding vectors
        """
        try:
            import httpx
        except ImportError:
            logger.error("httpx not installed for Voyage embeddings")
            raise ImportError("httpx required for Voyage embeddings")

        response = httpx.post(
            "https://api.voyageai.com/v1/embeddings",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model_name,
                "input": texts,
            },
            timeout=30.0,
        )

        response.raise_for_status()
        data = response.json()

        return [item["embedding"] for item in data["data"]]

    @property
    def dimension(self) -> int:
        """Get embedding dimension."""
        return self._dimension


class EmbeddingCache:
    """SQLite-based embedding cache to avoid recomputation."""

    def __init__(self, cache_path: Optional[Path] = None):
        """Initialize cache.

        Args:
            cache_path: Path to cache database
        """
        if cache_path is None:
            cache_path = Path.home() / ".glock" / "embedding_cache.db"

        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _init_db(self) -> None:
        """Initialize cache database."""
        with sqlite3.connect(self.cache_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    text_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_provider_model ON embeddings(provider, model)"
            )
            conn.commit()

    def get(
        self,
        text: str,
        provider: str,
        model: str,
    ) -> Optional[list[float]]:
        """Get cached embedding.

        Args:
            text: Original text
            provider: Provider name
            model: Model name

        Returns:
            Embedding if cached, None otherwise
        """
        text_hash = hashlib.sha256(text.encode()).hexdigest()

        with sqlite3.connect(self.cache_path) as conn:
            cursor = conn.execute(
                """
                SELECT embedding FROM embeddings
                WHERE text_hash = ? AND provider = ? AND model = ?
                """,
                (text_hash, provider, model),
            )
            row = cursor.fetchone()

            if row:
                return deserialize_embedding(row[0])
            return None

    def set(
        self,
        text: str,
        provider: str,
        model: str,
        embedding: list[float],
    ) -> None:
        """Cache an embedding.

        Args:
            text: Original text
            provider: Provider name
            model: Model name
            embedding: Embedding vector
        """
        text_hash = hashlib.sha256(text.encode()).hexdigest()

        with sqlite3.connect(self.cache_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO embeddings (text_hash, provider, model, embedding)
                VALUES (?, ?, ?, ?)
                """,
                (text_hash, provider, model, serialize_embedding(embedding)),
            )
            conn.commit()

    def clear(self, provider: Optional[str] = None, model: Optional[str] = None) -> int:
        """Clear cached embeddings.

        Args:
            provider: Optional provider filter
            model: Optional model filter

        Returns:
            Number of entries deleted
        """
        with sqlite3.connect(self.cache_path) as conn:
            if provider and model:
                cursor = conn.execute(
                    "DELETE FROM embeddings WHERE provider = ? AND model = ?",
                    (provider, model),
                )
            elif provider:
                cursor = conn.execute(
                    "DELETE FROM embeddings WHERE provider = ?",
                    (provider,),
                )
            else:
                cursor = conn.execute("DELETE FROM embeddings")

            conn.commit()
            return cursor.rowcount


class EmbeddingManager:
    """High-level embedding manager with caching and provider abstraction."""

    def __init__(self, config: Optional[EmbeddingConfig] = None):
        """Initialize embedding manager.

        Args:
            config: Embedding configuration
        """
        self.config = config or EmbeddingConfig()
        self._provider = self._create_provider()
        self._cache = EmbeddingCache() if self.config.cache_enabled else None

    def _create_provider(self) -> EmbeddingProvider:
        """Create embedding provider based on config.

        Returns:
            EmbeddingProvider instance
        """
        if self.config.provider == "openai":
            if not self.config.api_key:
                raise ValueError("OpenAI API key required")
            return OpenAIEmbeddingProvider(
                self.config.api_key,
                self.config.model_name,
            )
        elif self.config.provider == "voyage":
            if not self.config.api_key:
                raise ValueError("Voyage API key required")
            return VoyageEmbeddingProvider(
                self.config.api_key,
                self.config.model_name,
            )
        else:
            return LocalEmbeddingProvider(self.config.model_name)

    def embed(self, text: str) -> list[float]:
        """Generate embedding for text with caching.

        Args:
            text: Text to embed

        Returns:
            Embedding vector
        """
        # Check cache
        if self._cache:
            cached = self._cache.get(
                text,
                self.config.provider,
                self.config.model_name,
            )
            if cached:
                return cached

        # Generate embedding
        embedding = self._provider.embed_text(text)

        # Cache result
        if self._cache:
            self._cache.set(
                text,
                self.config.provider,
                self.config.model_name,
                embedding,
            )

        return embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts with caching.

        Args:
            texts: Texts to embed

        Returns:
            List of embedding vectors
        """
        # Check cache for each text
        results: list[Optional[list[float]]] = [None] * len(texts)
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        if self._cache:
            for i, text in enumerate(texts):
                cached = self._cache.get(
                    text,
                    self.config.provider,
                    self.config.model_name,
                )
                if cached:
                    results[i] = cached
                else:
                    uncached_indices.append(i)
                    uncached_texts.append(text)
        else:
            uncached_indices = list(range(len(texts)))
            uncached_texts = texts

        # Generate embeddings for uncached texts
        if uncached_texts:
            new_embeddings = self._provider.embed_batch(uncached_texts)

            for idx, text, embedding in zip(
                uncached_indices, uncached_texts, new_embeddings
            ):
                results[idx] = embedding
                if self._cache:
                    self._cache.set(
                        text,
                        self.config.provider,
                        self.config.model_name,
                        embedding,
                    )

        return [r for r in results if r is not None]  # type: ignore

    def similarity(self, text1: str, text2: str) -> float:
        """Compute similarity between two texts.

        Args:
            text1: First text
            text2: Second text

        Returns:
            Similarity score (0 to 1)
        """
        emb1 = self.embed(text1)
        emb2 = self.embed(text2)
        # Convert from cosine (-1 to 1) to (0 to 1)
        return (cosine_similarity(emb1, emb2) + 1) / 2

    def find_similar(
        self,
        query: str,
        candidates: list[str],
        top_k: int = 5,
    ) -> list[tuple[int, str, float]]:
        """Find most similar texts from candidates.

        Args:
            query: Query text
            candidates: List of candidate texts
            top_k: Number of results to return

        Returns:
            List of (index, text, score) tuples sorted by similarity
        """
        query_embedding = self.embed(query)
        candidate_embeddings = self.embed_batch(candidates)

        scores = []
        for i, (text, embedding) in enumerate(zip(candidates, candidate_embeddings)):
            score = (cosine_similarity(query_embedding, embedding) + 1) / 2
            scores.append((i, text, score))

        # Sort by score descending
        scores.sort(key=lambda x: x[2], reverse=True)

        return scores[:top_k]

    @property
    def dimension(self) -> int:
        """Get embedding dimension."""
        return self._provider.dimension
