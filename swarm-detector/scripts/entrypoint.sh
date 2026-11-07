#!/bin/sh
set -eu

# Creating an empty schema is the application's job. Never import or seed real data here.
if [ "$#" -eq 0 ]; then
    set -- gunicorn --config /app/gunicorn.conf.py swarm_detector.wsgi:server
fi

exec "$@"
