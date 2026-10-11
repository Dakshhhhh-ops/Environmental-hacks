#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
python scripts/package_frontend.py
