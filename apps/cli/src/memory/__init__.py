"""Persistent memory system for Glock CLI."""

from .store import MemoryStore, Memory
from .embeddings import (
    EmbeddingConfig,
    EmbeddingManager,
    EmbeddingProvider,
    LocalEmbeddingProvider,
    OpenAIEmbeddingProvider,
    VoyageEmbeddingProvider,
    EmbeddingCache,
    serialize_embedding,
    deserialize_embedding,
    cosine_similarity,
)

__all__ = [
    "MemoryStore",
    "Memory",
    "EmbeddingConfig",
    "EmbeddingManager",
    "EmbeddingProvider",
    "LocalEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "VoyageEmbeddingProvider",
    "EmbeddingCache",
    "serialize_embedding",
    "deserialize_embedding",
    "cosine_similarity",
]
