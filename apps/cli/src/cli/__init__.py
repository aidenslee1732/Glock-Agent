"""CLI commands.

Use lazy imports to avoid RuntimeWarning when running as __main__.
"""


def __getattr__(name: str):
    """Lazy import to avoid circular import issues."""
    if name in ("main", "cli"):
        from .main import main, cli
        return main if name == "main" else cli
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["main", "cli"]
