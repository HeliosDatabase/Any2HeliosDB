#!/usr/bin/env bash
# Any2HeliosDB cast demo: migrate the Pagila sample DB (PostgreSQL) -> HeliosDB-Nano.
# Driven by asciinema (see record.sh). Every command below is real.
set -u
cd /tmp/a2h-cast-demo
export PG_PW=postgres            # a2h-pg (source) password; Nano target is trust-auth
export HOSTNAME=host001          # hostname masked for the recording (real host hidden)
export USER=user01 LOGNAME=user01  # username masked for the recording

# --- cosmetics -------------------------------------------------------------
GREEN=$'\033[1;32m'; BLUE=$'\033[1;34m'; CYAN=$'\033[1;36m'; BOLD=$'\033[1m'; RST=$'\033[0m'
PROMPT="${GREEN}user01@host001${RST}:${BLUE}~/a2h-demo${RST}$ "
RULE="${CYAN}──────────────────────────────────────────────────────────────────────${RST}"

type_line() {                    # simulate typing for readability
  local s=$1 i
  for (( i=0; i<${#s}; i++ )); do printf '%s' "${s:$i:1}"; sleep 0.020; done
}
pe() {                           # print prompt, "type" the command, then run it
  printf '%s' "$PROMPT"; type_line "$1"; printf '\n'; sleep 0.4
  eval "$1"
  printf '\n'; sleep 1.3
}

clear
printf '%s\n' "$RULE"
printf '%s\n' "  ${BOLD}${CYAN}Any2HeliosDB${RST}  —  migrate ${BOLD}Pagila${RST} (PostgreSQL)  →  ${BOLD}HeliosDB-Nano${RST}"
printf '%s\n' "$RULE"
printf '\n'; sleep 1.6

# 1. Identity
pe "a2h --version"

# 2. What we're migrating (source = Pagila/PG, target = Nano over PG-wire)
pe "cat config.toml"

# 3. Preflight: local environment + available drivers
pe "a2h doctor"

# 4. The migration itself — schema + data, end to end
pe "a2h migrate -c config.toml"

# 5. Verify row counts on both sides
pe "a2h test-count -c config.toml"

# 6. Verify the data itself (ordered, sampled row compare + checksums)
pe "a2h test-data  -c config.toml --sample 200"

printf '%s\n' "$RULE"
printf '%s\n' "  ${GREEN}${BOLD}✅  Pagila → HeliosDB-Nano: 15 tables / 49,636 rows migrated & verified${RST}"
printf '%s\n' "$RULE"
sleep 2.5
