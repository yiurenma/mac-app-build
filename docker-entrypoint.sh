#!/bin/bash
# Web Console + scheduler in one container. The web server goes first: its start-up
# hand-over retires "old" services, so the scheduler must start after it.
set -u
python3 -m appbuilder web --no-open &
web=$!
sleep 8
python3 -m appbuilder scheduler run &
sched=$!
trap 'kill "$web" "$sched" 2>/dev/null' TERM INT
wait -n "$web" "$sched"
code=$?
kill "$web" "$sched" 2>/dev/null
exit "$code"
