#!/usr/bin/env bash

if [ -n "$DEBUG" ]; then
    set -ex
fi

SCRIPT="${SCRIPT:-smartmon.sh}"
OUTPUT_FILENAME="${OUTPUT_FILENAME:-${SCRIPT%.*}}"
OUTPUT_PATH="${OUTPUT_PATH:-/var/lib/node_exporter}"
INTERVAL="${INTERVAL:-300}"

if [ ! -f "/scripts/${SCRIPT}" ]; then
    echo "Script ${SCRIPT} doesn't exist. Exiting 1"
    exit 1
fi

echo "Starting ${SCRIPT} loop ..."
while true; do
    "/scripts/${SCRIPT}" "${@}" | sponge "${OUTPUT_PATH}/${OUTPUT_FILENAME}.prom"
    sleep "${INTERVAL}"
done