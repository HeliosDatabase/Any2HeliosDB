#!/usr/bin/env bash
# Record the Pagila -> Nano demo into an asciinema v2 .cast file.
set -eu
export TERM=xterm-256color
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export HOSTNAME=host001          # mask the real host for the recording
export USER=user01 LOGNAME=user01  # mask the real username for the recording

# Fresh manifest so the recorded migrate runs clean (no "resuming" noise).
rm -rf /tmp/a2h-cast-demo/out
mkdir -p /tmp/a2h-cast-demo/out

cd /tmp/a2h-cast-demo
asciinema rec --overwrite -q \
  --cols 100 --rows 30 \
  -i 2 \
  -t "Any2HeliosDB - Pagila (PostgreSQL) -> HeliosDB-Nano" \
  -c "bash /tmp/a2h-cast-demo/demo.sh" \
  /tmp/a2h-cast-demo/pagila-nano.cast

echo "wrote: /tmp/a2h-cast-demo/pagila-nano.cast"
