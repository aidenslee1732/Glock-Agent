"""Code Graph - Semantic code index for navigation.

Provides Amp-like code graph capabilities:
- Symbol indexing (functions, classes, variables)
- Reference finding (where is X used?)
- Definition finding (where is X defined?)
- Call graph analysis
- Import/dependency tracking
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SymbolType(str, Enum):
    """Types of symbols in the code graph."""
    FUNCTION = "function"
    CLASS = "class"
    METHOD = "method"
    VARIABLE = "variable"
    CONSTANT = "constant"
    IMPORT = "import"
    MODULE = "module"


@dataclass
class Symbol:
    """A symbol in the code graph."""

    name: str
    type: SymbolType
    file_path: str
    line_number: int
    column: int = 0
    end_line: Optional[int] = None
    docstring: Optional[str] = None
    signature: Optional[str] = None
    parent: Optional[str] = None  # Parent class/module
    visibility: str = "public"  # public, private, protected
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        """Get fully qualified name."""
        if self.parent:
            return f"{self.parent}.{self.name}"
        return self.name

    @property
    def location(self) -> str:
        """Get location string."""
        return f"{self.file_path}:{self.line_number}"


@dataclass
class Reference:
    """A reference to a symbol."""

    symbol_name: str
    file_path: str
    line_number: int
    column: int = 0
    context: str = ""  # Surrounding code snippet
    reference_type: str = "usage"  # usage, import, call, assignment


@dataclass
class Definition:
    """A symbol definition."""

    symbol: Symbol
    source: str  # Source code of the definition


@dataclass
class CallEdge:
    """An edge in the call graph."""

    caller: str  # Qualified name of caller
    callee: str  # Qualified name of callee
    file_path: str
    line_number: int


@dataclass
class CallGraph:
    """Call graph for a function."""

    root: str  # Root function name
    calls: list[CallEdge]  # Outgoing calls
    called_by: list[CallEdge]  # Incoming calls
    depth: int = 1  # Analysis depth


@dataclass
class IndexConfig:
    """Configuration for code indexing."""

    # File patterns to index
    include_patterns: list[str] = field(default_factory=lambda: [
        "**/*.py",
        "**/*.js",
        "**/*.ts",
        "**/*.jsx",
        "**/*.tsx",
    ])

    # Patterns to exclude
    exclude_patterns: list[str] = field(default_factory=lambda: [
        "**/node_modules/**",
        "**/.git/**",
        "**/venv/**",
        "**/__pycache__/**",
        "**/dist/**",
        "**/build/**",
    ])

    # Maximum file size to index (bytes)
    max_file_size: int = 1024 * 1024  # 1MB

    # Cache directory
    cache_dir: Optional[Path] = None


class CodeGraph:
    """Semantic code graph for navigation and analysis.

    Indexes code symbols and their relationships:
    - Function/class definitions
    - Variable declarations
    - Import statements
    - Call relationships
    """

    def __init__(
        self,
        workspace: Path,
        config: Optional[IndexConfig] = None,
    ):
        """Initialize code graph.

        Args:
            workspace: Workspace root path
            config: Index configuration
        """
        self.workspace = workspace.resolve()
        self.config = config or IndexConfig()

        # Symbol storage
        self._symbols: dict[str, Symbol] = {}  # qualified_name -> Symbol
        self._file_symbols: dict[str, list[str]] = {}  # file -> [qualified_names]
        self._references: dict[str, list[Reference]] = {}  # symbol_name -> [References]
        self._imports: dict[str, list[str]] = {}  # file -> [imported modules]
        self._calls: dict[str, list[CallEdge]] = {}  # caller -> [CallEdges]

        # Index metadata
        self._indexed_files: dict[str, str] = {}  # file -> content_hash
        self._last_full_index: Optional[datetime] = None

    async def index_workspace(self, incremental: bool = True) -> dict[str, int]:
        """Index the entire workspace.

        Args:
            incremental: If True, only index changed files

        Returns:
            Stats about indexed files
        """
        stats = {"indexed": 0, "skipped": 0, "failed": 0}

        # Find files to index
        files_to_index = await self._find_files_to_index(incremental)

        # Index files concurrently
        tasks = [
            self._index_file(file_path)
            for file_path in files_to_index
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                stats["failed"] += 1
            elif result:
                stats["indexed"] += 1
            else:
                stats["skipped"] += 1

        self._last_full_index = datetime.utcnow()
        logger.info(f"Indexed workspace: {stats}")
        return stats

    async def index_file(self, path: Path) -> None:
        """Index a single file.

        Args:
            path: Path to file
        """
        await self._index_file(path)

    async def find_references(self, symbol: str) -> list[Reference]:
        """Find all references to a symbol.

        Args:
            symbol: Symbol name to find references for

        Returns:
            List of references
        """
        # Check cached references
        if symbol in self._references:
            return self._references[symbol]

        # Search through all indexed files
        references = []
        for file_path, symbols in self._file_symbols.items():
            refs = await self._find_references_in_file(symbol, file_path)
            references.extend(refs)

        self._references[symbol] = references
        return references

    async def find_definition(self, symbol: str) -> Optional[Definition]:
        """Find the definition of a symbol.

        Args:
            symbol: Symbol name to find

        Returns:
            Definition if found
        """
        # Look up in symbol table
        if symbol in self._symbols:
            sym = self._symbols[symbol]
            source = await self._get_definition_source(sym)
            return Definition(symbol=sym, source=source)

        # Search by name (partial match)
        for qualified_name, sym in self._symbols.items():
            if sym.name == symbol or qualified_name.endswith(f".{symbol}"):
                source = await self._get_definition_source(sym)
                return Definition(symbol=sym, source=source)

        return None

    async def get_call_graph(
        self,
        function: str,
        depth: int = 2,
    ) -> CallGraph:
        """Get call graph for a function.

        Args:
            function: Function name
            depth: How many levels deep to analyze

        Returns:
            CallGraph with calls and called_by
        """
        calls = []
        called_by = []

        # Find outgoing calls
        if function in self._calls:
            calls = self._calls[function]

        # Find incoming calls (who calls this function)
        for caller, edges in self._calls.items():
            for edge in edges:
                if edge.callee == function:
                    called_by.append(edge)

        return CallGraph(
            root=function,
            calls=calls,
            called_by=called_by,
            depth=depth,
        )

    def get_symbol(self, name: str) -> Optional[Symbol]:
        """Get a symbol by name."""
        return self._symbols.get(name)

    def get_file_symbols(self, file_path: str) -> list[Symbol]:
        """Get all symbols in a file."""
        rel_path = str(Path(file_path).relative_to(self.workspace))
        qualified_names = self._file_symbols.get(rel_path, [])
        return [self._symbols[qn] for qn in qualified_names if qn in self._symbols]

    def search_symbols(
        self,
        query: str,
        symbol_type: Optional[SymbolType] = None,
        limit: int = 20,
    ) -> list[Symbol]:
        """Search for symbols by name.

        Args:
            query: Search query (supports fuzzy matching)
            symbol_type: Filter by type
            limit: Maximum results

        Returns:
            Matching symbols
        """
        matches = []
        query_lower = query.lower()

        for symbol in self._symbols.values():
            # Type filter
            if symbol_type and symbol.type != symbol_type:
                continue

            # Name match
            name_lower = symbol.name.lower()
            if query_lower in name_lower or name_lower.startswith(query_lower):
                matches.append(symbol)

        # Sort by relevance (exact match first, then by name length)
        matches.sort(key=lambda s: (
            0 if s.name.lower() == query_lower else 1,
            len(s.name),
        ))

        return matches[:limit]

    async def _find_files_to_index(self, incremental: bool) -> list[Path]:
        """Find files that need indexing."""
        files = []

        for pattern in self.config.include_patterns:
            for file_path in self.workspace.glob(pattern):
                # Check exclusions
                rel_path = file_path.relative_to(self.workspace)
                excluded = False
                for exc in self.config.exclude_patterns:
                    if file_path.match(exc):
                        excluded = True
                        break

                if excluded:
                    continue

                if not file_path.is_file():
                    continue

                # Check file size
                if file_path.stat().st_size > self.config.max_file_size:
                    continue

                # Check if file changed (incremental)
                if incremental:
                    content_hash = self._compute_file_hash(file_path)
                    if self._indexed_files.get(str(rel_path)) == content_hash:
                        continue

                files.append(file_path)

        return files

    async def _index_file(self, file_path: Path) -> bool:
        """Index a single file."""
        try:
            content = file_path.read_text()
            rel_path = str(file_path.relative_to(self.workspace))

            # Clear existing symbols for this file
            if rel_path in self._file_symbols:
                for qn in self._file_symbols[rel_path]:
                    self._symbols.pop(qn, None)
                self._file_symbols[rel_path] = []

            # Parse based on language
            suffix = file_path.suffix.lower()
            if suffix == ".py":
                symbols = self._parse_python(content, rel_path)
            elif suffix in (".js", ".jsx", ".ts", ".tsx"):
                symbols = self._parse_javascript(content, rel_path)
            else:
                return False

            # Store symbols
            qualified_names = []
            for symbol in symbols:
                qn = f"{rel_path}:{symbol.qualified_name}"
                self._symbols[qn] = symbol
                qualified_names.append(qn)

            self._file_symbols[rel_path] = qualified_names

            # Update file hash
            self._indexed_files[rel_path] = self._compute_file_hash(file_path)

            return True

        except Exception as e:
            logger.warning(f"Failed to index {file_path}: {e}")
            return False

    def _parse_python(self, content: str, file_path: str) -> list[Symbol]:
        """Parse Python file for symbols."""
        symbols = []

        try:
            tree = ast.parse(content)
        except SyntaxError:
            return symbols

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                # Get docstring
                docstring = ast.get_docstring(node)

                # Get signature
                args = [a.arg for a in node.args.args]
                signature = f"def {node.name}({', '.join(args)})"

                # Determine visibility
                visibility = "private" if node.name.startswith("_") else "public"

                symbols.append(Symbol(
                    name=node.name,
                    type=SymbolType.FUNCTION,
                    file_path=file_path,
                    line_number=node.lineno,
                    column=node.col_offset,
                    end_line=getattr(node, 'end_lineno', None),
                    docstring=docstring,
                    signature=signature,
                    visibility=visibility,
                ))

            elif isinstance(node, ast.ClassDef):
                docstring = ast.get_docstring(node)

                # Get base classes
                bases = [
                    getattr(b, 'id', getattr(getattr(b, 'attr', None), '__str__', lambda: '')())
                    for b in node.bases
                ]
                signature = f"class {node.name}({', '.join(str(b) for b in bases)})"

                symbols.append(Symbol(
                    name=node.name,
                    type=SymbolType.CLASS,
                    file_path=file_path,
                    line_number=node.lineno,
                    column=node.col_offset,
                    end_line=getattr(node, 'end_lineno', None),
                    docstring=docstring,
                    signature=signature,
                ))

                # Index methods
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        visibility = "private" if item.name.startswith("_") else "public"
                        symbols.append(Symbol(
                            name=item.name,
                            type=SymbolType.METHOD,
                            file_path=file_path,
                            line_number=item.lineno,
                            column=item.col_offset,
                            parent=node.name,
                            visibility=visibility,
                        ))

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    symbols.append(Symbol(
                        name=alias.asname or alias.name,
                        type=SymbolType.IMPORT,
                        file_path=file_path,
                        line_number=node.lineno,
                        column=node.col_offset,
                        metadata={"module": alias.name},
                    ))

            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    symbols.append(Symbol(
                        name=alias.asname or alias.name,
                        type=SymbolType.IMPORT,
                        file_path=file_path,
                        line_number=node.lineno,
                        column=node.col_offset,
                        metadata={"module": f"{module}.{alias.name}"},
                    ))

        return symbols

    def _parse_javascript(self, content: str, file_path: str) -> list[Symbol]:
        """Parse JavaScript/TypeScript file for symbols (basic regex-based)."""
        symbols = []
        lines = content.split("\n")

        for i, line in enumerate(lines, 1):
            # Function declarations
            match = re.match(
                r'(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(',
                line.strip()
            )
            if match:
                symbols.append(Symbol(
                    name=match.group(1),
                    type=SymbolType.FUNCTION,
                    file_path=file_path,
                    line_number=i,
                    column=0,
                ))
                continue

            # Arrow functions assigned to const/let/var
            match = re.match(
                r'(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>',
                line.strip()
            )
            if match:
                symbols.append(Symbol(
                    name=match.group(1),
                    type=SymbolType.FUNCTION,
                    file_path=file_path,
                    line_number=i,
                    column=0,
                ))
                continue

            # Class declarations
            match = re.match(
                r'(?:export\s+)?class\s+(\w+)',
                line.strip()
            )
            if match:
                symbols.append(Symbol(
                    name=match.group(1),
                    type=SymbolType.CLASS,
                    file_path=file_path,
                    line_number=i,
                    column=0,
                ))
                continue

        return symbols

    async def _find_references_in_file(
        self,
        symbol: str,
        file_path: str,
    ) -> list[Reference]:
        """Find references to a symbol in a file."""
        references = []

        try:
            full_path = self.workspace / file_path
            content = full_path.read_text()
            lines = content.split("\n")

            # Simple word boundary search
            pattern = re.compile(rf'\b{re.escape(symbol)}\b')

            for i, line in enumerate(lines, 1):
                for match in pattern.finditer(line):
                    references.append(Reference(
                        symbol_name=symbol,
                        file_path=file_path,
                        line_number=i,
                        column=match.start(),
                        context=line.strip(),
                    ))

        except Exception as e:
            logger.warning(f"Failed to search {file_path}: {e}")

        return references

    async def _get_definition_source(self, symbol: Symbol) -> str:
        """Get source code for a symbol definition."""
        try:
            full_path = self.workspace / symbol.file_path
            content = full_path.read_text()
            lines = content.split("\n")

            # Get lines for the definition
            start = symbol.line_number - 1
            end = (symbol.end_line or symbol.line_number + 10) - 1
            return "\n".join(lines[start:end + 1])

        except Exception:
            return ""

    def _compute_file_hash(self, file_path: Path) -> str:
        """Compute content hash for a file."""
        try:
            content = file_path.read_bytes()
            return hashlib.md5(content).hexdigest()
        except Exception:
            return ""

    def stats(self) -> dict[str, Any]:
        """Get index statistics."""
        return {
            "total_symbols": len(self._symbols),
            "indexed_files": len(self._indexed_files),
            "last_full_index": (
                self._last_full_index.isoformat()
                if self._last_full_index else None
            ),
            "symbol_types": {
                st.value: sum(1 for s in self._symbols.values() if s.type == st)
                for st in SymbolType
            },
        }

    def clear(self) -> None:
        """Clear the index."""
        self._symbols.clear()
        self._file_symbols.clear()
        self._references.clear()
        self._imports.clear()
        self._calls.clear()
        self._indexed_files.clear()
        self._last_full_index = None
