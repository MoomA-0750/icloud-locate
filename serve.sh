#!/usr/bin/env bash
# icloud-locate の常駐HTTP APIを起動する薄いラッパー
cd "$(dirname "$0")"
exec ./.venv/bin/python ./server.py "$@"
