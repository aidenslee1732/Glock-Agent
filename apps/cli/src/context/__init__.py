"""Context management for Model B client.

v4: Added accurate tokenization support.
"""

from .packer import ContextPacker
from .budget import TokenBudgetManager
from .compressor import ToolOutputCompressor
from .slicer import SelectiveFileSlicer
from .summary import RollingSummaryManager
from .facts import PinnedFactsManager
from .delta import DeltaBuilder
from .tokenizer import (
    AccurateTokenizer,
    get_tokenizer,
    count_tokens,
    estimate_tokens,
)

__all__ = [
    "ContextPacker",
    "TokenBudgetManager",
    "ToolOutputCompressor",
    "SelectiveFileSlicer",
    "RollingSummaryManager",
    "PinnedFactsManager",
    "DeltaBuilder",
    # v4: Tokenization
    "AccurateTokenizer",
    "get_tokenizer",
    "count_tokens",
    "estimate_tokens",
]
