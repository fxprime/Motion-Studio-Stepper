#!/bin/sh
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
    echo "Install the app first using the Start section in README.md."
    exit 1
fi
exec .venv/bin/python -m showcase "$@"
