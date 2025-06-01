#!/usr/bin/env bash

if [ -n "$DEBUG" ]; then
    set -ex
fi

if [ -z "$SCRIPTS" ] && [ -n "$SCRIPT" ]; then
    SCRIPTS="$SCRIPT"
fi
SCRIPTS="${SCRIPTS:-smartmon.py}"
OUTPUT_PATH="${OUTPUT_PATH:-/var/lib/node_exporter}"
INTERVAL="${INTERVAL:-300}"

IFS=',' read -ra SCRIPT_LIST <<< "$SCRIPTS"

for SCRIPT in "${SCRIPT_LIST[@]}"; do
    if [ ! -f "/scripts/${SCRIPT}" ]; then
        echo "Script ${SCRIPT} doesn't exist. Exiting 1"
        exit 1
    fi
    OUTPUT_FILENAME="${SCRIPT%.*}"
    echo "Prepared to run ${SCRIPT} -> ${OUTPUT_PATH}/${OUTPUT_FILENAME}.prom"
done

echo "Starting scripts loop ..."
while true; do
    for SCRIPT in "${SCRIPT_LIST[@]}"; do
        OUTPUT_FILENAME="${SCRIPT%.*}"
        "/scripts/${SCRIPT}" "$@" | sponge "${OUTPUT_PATH}/${OUTPUT_FILENAME}.prom"
    done
    sleep "${INTERVAL}"
done