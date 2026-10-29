#!/usr/bin/env bash
cd "$(dirname "$0")"
# shellcheck disable=SC1091
. .venv/bin/activate
exec python buddy.py "$@"
