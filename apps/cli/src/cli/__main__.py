"""Entry point for python -m apps.cli.src.cli"""

try:
    from apps.cli.src.cli.main import main
except ImportError:
    from .main import main

if __name__ == "__main__":
    main()
