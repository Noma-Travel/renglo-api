"""
Renglo API
"""

from .app import create_app, run

__version__ = "1.0.0"
__all__ = ["create_app", "run", "app"]


def __getattr__(name: str):
    if name == "app":
        from .app import get_app

        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

