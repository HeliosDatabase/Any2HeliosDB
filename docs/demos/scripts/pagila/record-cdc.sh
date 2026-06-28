#!/usr/bin/env bash
# Record the no-downtime CDC cast (Pagila -> Nano). Resets to a clean state first:
# a fresh source DB (new slot, actor back to 200) and a fresh empty Nano.
set -u
export TERM=xterm-256color LANG=C.UTF-8 LC_ALL=C.UTF-8
export HOSTNAME=host001 USER=user01 LOGNAME=user01

# 1. fresh source DB (drops the old logical slot, actor table back to 200 rows)
docker exec a2h-pg psql -U postgres -c "DROP DATABASE IF EXISTS pagila_cdc_demo WITH (FORCE);" >/dev/null 2>&1
docker exec a2h-pg psql -U postgres -c "CREATE DATABASE pagila_cdc_demo TEMPLATE pagila;" >/dev/null 2>&1

# 2. fresh empty Nano on :55432 (so the initial migrate has nothing to drop)
npid=$(ss -ltnp 2>/dev/null | grep ':55432' | grep -oP 'pid=\K[0-9]+' | head -1)
if [ -n "${npid:-}" ]; then kill -9 "$npid" "$(ps -o ppid= -p "$npid" | tr -d ' ')" 2>/dev/null || true; fi
sleep 2; rm -rf /tmp/a2h-nano3604-data-cdc
nohup /tmp/nano3604/bin/heliosdb-nano start --data-dir /tmp/a2h-nano3604-data-cdc \
      --port 55432 --listen 127.0.0.1 --auth trust --http-port 55434 >/dev/null 2>&1 </dev/null &
n=0; until docker run --rm --network host postgres:16 psql -h127.0.0.1 -p55432 -Upostgres -dheliosdb -tAc "select 1" >/dev/null 2>&1; do
  n=$((n+1)); [ $n -gt 40 ] && { echo "nano failed to start"; exit 1; }; sleep 1; done

rm -rf /tmp/a2h-cast-demo/out-cdc
cd /tmp/a2h-cast-demo
asciinema rec --overwrite -q --cols 100 --rows 32 -i 2 \
  -t "Any2HeliosDB - zero-downtime CDC: Pagila (PostgreSQL) -> HeliosDB-Nano" \
  -c "bash /tmp/a2h-cast-demo/demo-cdc.sh" \
  /tmp/a2h-cast-demo/pagila-nano-cdc.cast
echo "wrote: /tmp/a2h-cast-demo/pagila-nano-cdc.cast"
