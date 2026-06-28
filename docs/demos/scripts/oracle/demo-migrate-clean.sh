#!/usr/bin/env bash
# Any2HeliosDB cast: one-shot migrate — Oracle HR -> HeliosDB-Nano. Real commands.
set -u
cd /tmp/a2h-cast-oracle-cdc
export PATH="/tmp/a2h-cast-oracle-cdc/bin:$PATH"   # nq = psql -> HeliosDB-Nano
export ORA_PW=hr
export HOSTNAME=host001 USER=user01 LOGNAME=user01
FAST="${DEMO_FAST:-0}"

GREEN=$'\033[1;32m'; BLUE=$'\033[1;34m'; CYAN=$'\033[1;36m'; YELLOW=$'\033[0;33m'; BOLD=$'\033[1m'; RST=$'\033[0m'
PROMPT="${GREEN}user01@host001${RST}:${BLUE}~/a2h-demo${RST}$ "
RULE="${CYAN}────────────────────────────────────────────────────────────────────────────────────${RST}"

nap()       { [ "$FAST" = "1" ] || sleep "$1"; }
type_line() { local s=$1 i; if [ "$FAST" = "1" ]; then printf '%s' "$s"; else for (( i=0; i<${#s}; i++ )); do printf '%s' "${s:$i:1}"; sleep 0.018; done; fi; }
pe()        { printf '%s' "$PROMPT"; type_line "$1"; printf '\n'; nap 0.4; eval "$1"; printf '\n'; nap 1.3; }
note()      { printf '%s\n\n' "${YELLOW}# $*${RST}"; nap 1.0; }

clear
printf '%s\n' "$RULE"
printf '%s\n' "  ${BOLD}${CYAN}Any2HeliosDB${RST}  —  migrate ${BOLD}Oracle HR${RST}  →  ${BOLD}HeliosDB-Nano${RST}"
printf '%s\n' "$RULE"
printf '\n'; nap 1.4

pe "a2h --version"
pe "cat a2h.toml"
pe "a2h doctor"

note "Schema + data, end to end."
pe "a2h migrate -c a2h.toml"

note "Validate: row counts, then target FK-index health."
pe "a2h test-count -c a2h.toml"
pe "a2h test-index -c a2h.toml"

note "Peek on Nano — Unicode, decimals and the FK → department all carried across:"
pe "nq -c \"SELECT e.emp_id, e.full_name, e.salary, d.dept_name FROM employees e LEFT JOIN departments d ON e.dept_id = d.dept_id ORDER BY e.emp_id\""

printf '%s\n' "$RULE"
printf '%s\n' "  ${GREEN}${BOLD}✅  Oracle HR → HeliosDB-Nano: schema + data migrated & verified${RST}"
printf '%s\n' "$RULE"
nap 2.5
