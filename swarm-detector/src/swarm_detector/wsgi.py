"""Explicit Gunicorn startup target; importing the web factory has no side effects."""

from .web import create_app

app = create_app()
server = app.server
