"""Terminal User Interface components.

Phase 4 TUI improvements:
- 4.1 Parallel agent progress visualization
- 4.2 Collapsible tool output
- 4.3 Live token counter
- 4.4 Explore agent panel
- 4.5 Progress bars
- 4.6 Improved diff preview
- 4.7 File tree view
- 4.8 Status line
"""

from .app import GlockTUI
from .components import (
    ParallelProgressTracker,
    StatusLine,
    FileTreeView,
    CollapsibleOutput,
    ProgressTracker,
    ExploreAgentPanel,
    ToolStatus,
    ToolExecution,
    ParallelBatch,
)

__all__ = [
    "GlockTUI",
    # Phase 4 components
    "ParallelProgressTracker",
    "StatusLine",
    "FileTreeView",
    "CollapsibleOutput",
    "ProgressTracker",
    "ExploreAgentPanel",
    "ToolStatus",
    "ToolExecution",
    "ParallelBatch",
]
