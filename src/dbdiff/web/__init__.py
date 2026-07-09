"""Local web app (FastAPI + HTMX) for browsing diff runs."""

from .app import create_app

__all__ = ["create_app"]
