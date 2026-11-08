"""WSGI entry point for Railpack Python builder."""

from traced_wsgi import app

# This is the WSGI application that Railpack will detect
application = app.server
