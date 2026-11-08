#!/usr/bin/env python3
"""Railpack entry point for Traced integrated workspace."""

import os
from traced_wsgi import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
