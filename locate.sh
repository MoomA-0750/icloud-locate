#!/usr/bin/env bash
# iCloud (Find My) から自分のApple端末の位置を取得する薄いラッパー
cd "$(dirname "$0")"
exec ./.venv/bin/python ./icloud_locate.py "$@"
