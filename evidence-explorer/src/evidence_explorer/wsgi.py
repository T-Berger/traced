"""Explicit deployment entry point; importing web helpers never starts the app."""

from .web import create_app

app = create_app()
server = app.server
