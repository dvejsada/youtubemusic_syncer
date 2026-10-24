#!/bin/sh
# Make sure the data volume is writable for the unprivileged app user, then drop root.
# (A bind mount such as ./data is created by Docker as root:root, which a non-root process cannot write to.)
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p "${DATA_DIR:-/data}"
    chown -R app:app "${DATA_DIR:-/data}" 2>/dev/null || true
    exec setpriv --reuid=app --regid=app --init-groups -- "$@"
fi
exec "$@"
