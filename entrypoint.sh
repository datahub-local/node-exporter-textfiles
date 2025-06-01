#!/usr/bin/env bash

if [ -n "$DEBUG" ]; then
    set -ex
fi

SCRIPTS_PATH="${SCRIPTS_PATH:-/scripts}"
OUTPUT_PATH="${OUTPUT_PATH:-/var/lib/node_exporter}"
INTERVAL="${INTERVAL:-300}"

PYTHON_SCRIPTS=("${SCRIPTS_PATH}"/*.py)
if [ ${#PYTHON_SCRIPTS[@]} -eq 0 ]; then
    echo "No Python scripts found in ${SCRIPTS_PATH}. Exiting 1"
    exit 1
fi

echo "Starting Python scripts loop ..."
while true; do
    for script in "${PYTHON_SCRIPTS[@]}"; do
        script_name=$(basename "$script" .py)
        "$script" "${@}" | sponge "${OUTPUT_PATH}/${script_name}.prom"
    done
    sleep "${INTERVAL}"
done