#!/bin/sh
# First start: copy the template project (input/, network/) into the data volume. Later starts keep the volume as it is.
set -e
if [ ! -d "$ADDRESS_PROJECT/input" ]; then
  mkdir -p "$ADDRESS_PROJECT"
  cp -r "$ADDRESS_TEMPLATE"/input "$ADDRESS_TEMPLATE"/network "$ADDRESS_PROJECT"/
  echo "project created in $ADDRESS_PROJECT from $ADDRESS_TEMPLATE"
fi
# ONE worker: the computed network lives in memory. Put a reverse proxy with TLS in front (see deploy/README.md).
exec python -m osmaddresssystem.app serve --project "$ADDRESS_PROJECT" --host 0.0.0.0 --port "${PORT:-8000}"
