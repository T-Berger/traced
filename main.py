#!/usr/bin/env python3
"""Main entry point for Railway deployment.

This file is used by Railpack to start the Traced integrated workspace.
It delegates to the WSGI application in traced_wsgi.py.
"""

import os
from traced_wsgi import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
