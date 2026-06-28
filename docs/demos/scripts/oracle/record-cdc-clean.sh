#!/usr/bin/env bash
# Reset to a clean state (Oracle HR baseline + fresh empty Nano), then record the
# clean CDC cast. The resets are silent (not part of the .cast).
set -uo pipefail
DIR=/tmp/a2h-cast-oracle-cdc; NANO=/tmp/nano3604/bin/heliosdb-nano
DATADIR=/tmp/a2h-oracle-cdc-nano-data; PORT=55436
export TERM=xterm-256color LANG=C.UTF-8 LC_ALL=C.UTF-8 HOSTNAME=host001 USER=user01 LOGNAME=user01

docker exec -i a2h-oracle sqlplus -S hr/hr@XEPDB1 >/dev/null 2>&1 <<'SQL'
DELETE FROM employees WHERE emp_id = 6;
UPDATE employees SET salary = 125000.5 WHERE emp_id = 1;
COMMIT;
SQL

MYPID=$(ss -ltnp 2>/dev/null | grep "127.0.0.1:$PORT" | grep -oP 'pid=\K[0-9]+' | head -1 || true)
if [ -n "${MYPID:-}" ]; then kill -9 "$MYPID" 2>/dev/null || true; sleep 2; fi
rm -rf "$DATADIR" "$DIR/out"; mkdir -p "$DIR/out"
nohup "$NANO" start --data-dir "$DATADIR" --port "$PORT" --listen 127.0.0.1 --auth trust \
      --http-port 55438 >"$DIR/nano-record.log" 2>&1 </dev/null &
for i in $(seq 1 60); do
  docker run --rm --network host postgres:16 psql -h127.0.0.1 -p"$PORT" -Upostgres -dheliosdb -tAc "select 1" >/dev/null 2>&1 && break
  sleep 1
done

asciinema rec --overwrite -q --cols 100 --rows 32 -i 2 \
  -t "Any2HeliosDB - zero-downtime CDC: Oracle HR -> HeliosDB-Nano" \
  -c "bash $DIR/demo-cdc-clean.sh" \
  "$DIR/oracle-nano-cdc.cast"
echo "wrote: $DIR/oracle-nano-cdc.cast"
