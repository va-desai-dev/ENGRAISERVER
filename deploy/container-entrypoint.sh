#!/bin/sh
set -eu

if [ "$#" -eq 0 ]; then
    set -- engrai-server serve
elif [ "${1#-}" != "$1" ]; then
    set -- engrai-server serve "$@"
fi

if [ "${ENGRAI_CONTAINER_INIT:-1}" = "1" ]; then
    engrai-server init >/dev/null

    if [ "${ENGRAI_INSTALL_BUNDLED_RUNTIMES:-1}" = "1" ]; then
        for bundle in /opt/engrai/runtime-bundles/*.tar.gz; do
            [ -f "$bundle" ] || continue
            engrai-server runtime install "$bundle"
        done
    fi
fi

exec "$@"
