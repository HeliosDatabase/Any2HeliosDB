#!/usr/bin/env bash
# Any2HeliosDB cast: NO-DOWNTIME migration via CDC — Oracle HR -> HeliosDB-Nano.
# Driven by asciinema (see record-cdc-clean.sh). Every command below is real.
set -u
cd /tmp/a2h-cast-oracle-cdc
export PATH="/tmp/a2h-cast-oracle-cdc/bin:$PATH"   # oq = sqlplus -> Oracle HR ; nq = psql -> Nano
export ORA_PW=hr
export HOSTNAME=host001 USER=user01 LOGNAME=user01   # identities masked for the recording
FAST="${DEMO_FAST:-0}"

GREEN=$'\033[1;32m'; BLUE=$'\033[1;34m'; CYAN=$'\033[1;36m'; YELLOW=$'\033[0;33m'; BOLD=$'\033[1m'; RST=$'\033[0m'
PROMPT="${GREEN}user01@host001${RST}:${BLUE}~/a2h-demo${RST}$ "
RULE="${CYAN}────────────────────────────────────────────────────────────────────────────────────${RST}"

nap()       { [ "$FAST" = "1" ] || sleep "$1"; }
type_line() { local s=$1 i; if [ "$FAST" = "1" ]; then printf '%s' "$s"; else for (( i=0; i<${#s}; i++ )); do printf '%s' "${s:$i:1}"; sleep 0.016; done; fi; }
pe()        { printf '%s' "$PROMPT"; type_line "$1"; printf '\n'; nap 0.4; eval "$1"; printf '\n'; nap 1.2; }
note()      { printf '%s\n\n' "${YELLOW}# $*${RST}"; nap 1.0; }

clear
printf '%s\n' "$RULE"
printf '%s\n' "  ${BOLD}${CYAN}Any2HeliosDB${RST}  —  ${BOLD}zero-downtime${RST} migration via ${BOLD}CDC${RST}:  Oracle HR  →  HeliosDB-Nano"
printf '%s\n' "$RULE"
printf '\n'; nap 1.3
note "Oracle SCN-watermark CDC: bulk-load once, keep the source LIVE; INSERT/UPDATE captured by SCN, DELETE via reconcile."
note "Helpers: oq = sqlplus into Oracle HR · nq = psql into HeliosDB-Nano"

pe "cat a2h.toml"

note "1) Initial bulk load. The source stays online the whole time (zero downtime)."
pe "a2h migrate -c a2h.toml"
pe "a2h test-count -c a2h.toml"

note "2) Start CDC — record the SCN watermark baseline."
pe "a2h extract hr_cdc -c a2h.toml"

note "3) The live application keeps writing to Oracle — INSERT + UPDATE:"
pe "cat crud1.sql"
pe "oq < crud1.sql"

note "4) Capture the changes since the watermark and apply them to Nano."
pe "a2h extract hr_cdc -c a2h.toml"
pe "a2h replicat hr_cdc -c a2h.toml"
note "   verify on Nano — emp 6 inserted, emp 1 salary updated to 130000:"
pe "nq -tAc \"SELECT emp_id, full_name, salary FROM employees WHERE emp_id IN (1,6) ORDER BY emp_id\""

note "5) Now the app DELETEs a row. SCN capture can't see deletes — the reconcile pass will:"
pe "cat crud2.sql"
pe "oq < crud2.sql"
pe "a2h extract hr_cdc -c a2h.toml"
pe "a2h replicat hr_cdc -c a2h.toml"

note "6) Final parity — Nano matches the live source exactly (emp 6 gone again):"
pe "a2h test-count -c a2h.toml"
pe "nq -tAc \"SELECT count(*) AS rows, string_agg(emp_id::text, ',' ORDER BY emp_id) AS ids FROM employees\""

printf '%s\n' "$RULE"
printf '%s\n' "  ${GREEN}${BOLD}✅  Zero-downtime CDC: INSERT + UPDATE (SCN) + DELETE (reconcile) all replicated to HeliosDB-Nano${RST}"
printf '%s\n' "$RULE"
nap 2.5
