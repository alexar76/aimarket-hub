#!/bin/sh
# Entrypoint of the standalone image (Dockerfile.standalone).
#
# The hub runs as the unprivileged `hub` user. Platform volumes (Railway, Render, Fly) are
# mounted root-owned, so a hub that starts as `hub` cannot write its keys, database or
# settings there and dies on the first start. Started as root, this script hands /app/data
# to `hub` and then drops to it; nothing of the hub ever runs as root. Started as `hub`
# (docker run --user, or a platform that honours USER) it simply runs the hub.
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p /app/data
    chown -R hub:hub /app/data
    exec setpriv --reuid=hub --regid=hub --init-groups python -m aimarket_hub "$@"
fi
exec python -m aimarket_hub "$@"
