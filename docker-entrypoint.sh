#!/bin/bash
# `web` also starts (and shares) the single scheduler, so one foreground process is enough.
exec python3 -m appbuilder web --no-open
