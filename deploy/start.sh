#!/bin/sh
# Start the Traced workbench with gunicorn on $PORT.
#
# A Railway volume is mounted as root. When started as root, take ownership of the
# data directory and restart this script as the unprivileged traced user.
# TRACED_SEED_DEMO=1 imports the labelled synthetic demo cases once per data
# directory; nothing is seeded otherwise.
set -eu

data="${EVIDENCE_DATA_DIR:-/app/data}"
swarm="${SWARM_DATA_DIR:-$data/swarm}"
storm_db="${SILENT_STORM_DB:-$data/silent-storm/silent-storm.sqlite3}"
mkdir -p "$data" "$swarm" "$(dirname "$storm_db")"

if [ "$(id -u)" = "0" ]; then
    chown -R traced:traced "$data" "$swarm" "$(dirname "$storm_db")"
    exec setpriv --reuid=traced --regid=traced --init-groups "$0" "$@"
fi

if [ "${TRACED_SEED_DEMO:-}" = "1" ] && [ ! -e "$data/.demo-seeded" ]; then
    echo "Seeding the labelled synthetic demo cases into $data"
    python -m evidence_explorer.review.integrations --data-dir "$data" seed
    touch "$data/.demo-seeded"
fi

cd /app
exec gunicorn --config /app/deploy/gunicorn.conf.py traced_wsgi:application
