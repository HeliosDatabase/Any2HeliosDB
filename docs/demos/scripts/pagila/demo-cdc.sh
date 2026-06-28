#!/usr/bin/env bash
# Any2HeliosDB cast: NO-DOWNTIME migration via CDC — Pagila (PostgreSQL) -> HeliosDB-Nano.
# Driven by asciinema (see record-cdc.sh). Every command below is real.
set -u
cd /tmp/a2h-cast-demo
export PATH="/tmp/a2h-cast-demo/bin:$PATH"   # sq = psql -> Pagila source ; nq = psql -> Nano
export PG_PW=postgres
export HOSTNAME=host001 USER=user01 LOGNAME=user01   # identities masked for the recording

GREEN=$'\033[1;32m'; BLUE=$'\033[1;34m'; CYAN=$'\033[1;36m'; YELLOW=$'\033[0;33m'; BOLD=$'\033[1m'; RST=$'\033[0m'
PROMPT="${GREEN}user01@host001${RST}:${BLUE}~/a2h-demo${RST}$ "
RULE="${CYAN}──────────────────────────────────────────────────────────────────────────────${RST}"

type_line() { local s=$1 i; for (( i=0; i<${#s}; i++ )); do printf '%s' "${s:$i:1}"; sleep 0.018; done; }
pe()   { printf '%s' "$PROMPT"; type_line "$1"; printf '\n'; sleep 0.4; eval "$1"; printf '\n'; sleep 1.2; }
note() { printf '%s\n\n' "${YELLOW}# $*${RST}"; sleep 1.1; }

clear
printf '%s\n' "$RULE"
printf '%s\n' "  ${BOLD}${CYAN}Any2HeliosDB${RST}  —  ${BOLD}zero-downtime${RST} migration via ${BOLD}CDC${RST}:  Pagila (PostgreSQL)  →  HeliosDB-Nano"
printf '%s\n' "$RULE"
printf '\n'; sleep 1.4
note "Log-based CDC (PostgreSQL logical decoding): bulk-load once, keep the source LIVE, stream every change."
note "Helpers: sq = psql into the Pagila source · nq = psql into HeliosDB-Nano"

pe "cat config-cdc.toml"

note "1) Start change capture FIRST — a logical-decoding slot — so nothing is missed during/after the load."
pe "a2h extract pagila_cdc -c config-cdc.toml"

note "2) Initial bulk load. The source stays online the whole time (zero downtime)."
pe "a2h migrate -c config-cdc.toml"
pe "a2h test-count -c config-cdc.toml"

note "3) Meanwhile the live application keeps writing to Pagila — INSERT, UPDATE and DELETE:"
pe "cat crud.sql"
pe "sq < crud.sql"

note "4) Capture those changes straight from the WAL and apply them to Nano — including the DELETE."
pe "a2h extract pagila_cdc -c config-cdc.toml"
pe "a2h replicat pagila_cdc -c config-cdc.toml --no-deletes"

note "5) Target is back in sync — verify parity, then prove each CRUD op landed on Nano:"
pe "a2h test-count -c config-cdc.toml"
pe "nq -tA < verify-nano.sql"

printf '%s\n' "$RULE"
printf '%s\n' "  ${GREEN}${BOLD}✅  Zero-downtime CDC: INSERT + UPDATE + DELETE all replicated to HeliosDB-Nano — cut over anytime${RST}"
printf '%s\n' "$RULE"
sleep 2.5
