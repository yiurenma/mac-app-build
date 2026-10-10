#!/bin/bash
set -e
cd "$(dirname "$0")"
echo 'Import Previous APP Builder Tasks (no Git, no Jenkins Build)'
python3 -m appbuilder.migrate "$@"
echo 'Import completed. Now double-click Start APP Builder.command.'
