# CDC operations runbook

This is the **operator's** guide to running the a2h CDC spine unattended:
scheduling capture/apply, watching lag, reclaiming disk, triaging poison records,
adopting new tables, tearing an extract down, and recovering from the fail-closed
errors. For the *design* — the Extract → trail → Replicat model, watermark/cursor
semantics, idempotency, the keymove barrier, and the change-record format — read
[docs/cdc.md](../cdc.md) first; this page assumes it.

Every tunable named here is a [`[cdc]` config key](configuration.md#cdc-change-data-capture-tuning);
every action is a CLI flag with MCP parity. Command signatures are in the
[CLI reference](../reference/cli.md#change-data-capture); runnable cycles are in
[worked examples, scenario 5](examples.md#scenario-5).

## Scheduling capture and apply

Capture (`extract`) and apply (`replicat`) advance on **independent durable
cursors**, so run them on whatever cadence you like — even on different hosts that
share the trail directory. Each command does one bounded pass and exits, so drive
them from cron / a systemd timer rather than a loop:

```cron
# capture every minute; apply every minute, offset 30s so the two don't
# overlap I/O (cron has no sub-minute field, hence the sleep)
* * * * *  cd /srv/mig && a2h extract  cdc1 -c config.toml >> extract.log  2>&1
* * * * *  cd /srv/mig && sleep 30 && a2h replicat cdc1 -c config.toml >> replicat.log 2>&1
```

**Run exactly one `extract` process per extract name.** Trails take no
inter-process lock across *capture* runs, so two concurrent `a2h extract NAME`
would interleave appends and race the position file. Concurrent extracts of
*different* names are fine (every trail, cursor, and position file is per-name). A
`replicat` run and `--purge-applied` **do** take an advisory `trail.lock` and fail
fast if another already holds it, so those never corrupt each other.

Keep the schedule realistic: if capture can't keep up, changes queue in the source
log (WAL / binlog) and lag grows — watch it (below). The per-cycle working set is
bounded by [`capture_batch`](configuration.md#cdc-change-data-capture-tuning)
(events pulled per capture) and [`apply_batch`](configuration.md#cdc-change-data-capture-tuning)
(trail lines applied per chunk), so a large backlog drains over several cycles
instead of materializing in memory.

## Monitoring: state and lag

`a2h extracts -c config.toml` lists every extract with its schema, table count,
capture watermark, apply cursor, state (`registered` → `capturing` → `applying`),
and — when any are parked — a `dead_letters=N` count. It reads the registry only,
so it needs no source connection.

Add `--lag` to also measure how far behind each extract is (this **does** query the
source, best-effort — an unreachable source prints `lag: unavailable` rather than
failing the listing):

| Source | Metric | Meaning |
|---|---|---|
| **PostgreSQL** | `bytes_behind` | WAL the slot still pins: slot `confirmed_flush_lsn` vs `pg_current_wal_lsn()`. A steadily climbing value means apply/capture is falling behind and the slot is holding WAL on the source disk. |
| **MySQL** | `files_behind` + `bytes_behind` | Trailed binlog coordinate vs the server head. Byte offsets only compare within one binlog file, so across a rollover `files_behind` counts whole files between and `bytes_behind` is the head's offset in its own file. |
| **Oracle** | `scn_behind` | Current SCN minus the capture watermark SCN. A coarse "how many SCNs since we last captured". |

```
$ a2h extracts -c config.toml --lag
  cdc1             schema=HR tables=2 watermark=2547990 cursor=10 state=applying
      lag: mode=scn watermark_scn=2547990 current_scn=2548400 scn_behind=410
```

Over MCP: the `extracts` tool takes a `lag` boolean and reports each extract's
`dead_letters` count.

## Trail retention: rotation and reclaiming disk

The trail is append-only and, left alone, grows forever. With
[`trail_rotate_mb`](configuration.md#cdc-change-data-capture-tuning) set (default
256), the trail is split into size-bounded **segments**: `trail.jsonl` is segment
0 and rotated segments are `trail.00001.jsonl`, `trail.00002.jsonl`, … The active
segment is always the highest-numbered one. The apply **cursor stays a single
global line index** across every segment, so a legacy single-file trail and its
integer cursor keep working byte-for-byte; `0` disables rotation.

Reclaim disk with `a2h extract NAME --purge-applied` (MCP: `purge_applied: true`).
It deletes only **fully-applied, closed** segments — never the active one, never
any segment holding a line past the apply cursor — so it can never drop an
un-applied change. The count of removed lines is persisted in **`trail.meta`**
(`purged_lines`) so the global cursor stays valid after the purge. It is a
**manual** verb (there is no automatic purge); schedule it if disk is tight, but
only after apply has caught up:

```bash
a2h replicat cdc1 -c config.toml        # drain the trail first
a2h extract  cdc1 --purge-applied -c config.toml
#   extract cdc1: purged 3 applied segment(s) at cursor 41000
```

Purging is crash-safe (meta is written before each file is removed) and takes the
same `trail.lock` as a replicat run, so never run it while a `replicat` for the
same name is in flight — it fails fast with a clear "locked by another process"
error if you try.

## Dead-letter triage

A single record a *healthy* target rejects [`poison_retries`](configuration.md#cdc-change-data-capture-tuning)
times (default 3) is moved to **`dead_letter.jsonl`** in the trail directory and
the apply cursor advances past it, so one bad record can't wedge replication
forever. `a2h replicat` reports `dead-lettered N poison record(s)` and `a2h
extracts` shows `dead_letters=N`.

Each line is one JSON object:

```json
{"ts":"2026-07-11T12:00:00Z","reason":"<exception text>","cursor":10417,
 "op":"U","schema":"HR","table":"EMPLOYEES","source_pos":[...],
 "record":{ ...the full ChangeRecord... }}
```

- **`cursor`** is the failing record's global trail line — locate it in the trail
  with that index.
- **`reason`** is the target's exception (a constraint the row can never satisfy,
  an unparseable value).
- **`record`** is the complete change, so you can replay it by hand after fixing
  the cause.

The replicat **will not** re-apply a parked record; triage is manual: read the
line, fix the target or the data, and apply that one change yourself (or
re-snapshot the table). Three guarantees bound the blast radius:

- **A down target never dead-letters the backlog.** Before parking a record the
  replicat `ping()`s the target; a failed ping means the record failed because the
  target is *down*, so it **raises with the cursor unmoved** (the next run retries)
  instead of parking everything a transient outage rejected.
- **Mass poison fails closed** ([`poison_max_per_run`](configuration.md#cdc-change-data-capture-tuning),
  default 25): if one run would dead-letter more than this many records it raises
  instead — a flood is almost always an environment fault (wrong target, schema
  drift), not bad data. After you have investigated, a re-run skips the
  already-parked records (deduped by `source_pos`) and proceeds.
- **Key-moves are never dead-lettered** (skipping one diverges key state), so a
  key-move failure always fails closed — fix the cause and re-run.

Set `poison_retries = 0` to disable the policy entirely (a failing record raises,
the pre-hardening fail-closed behaviour).

## Adopting a table that appeared after registration

The captured table set is **pinned** at the first `extract`. A table created in
the source *later* is not silently absorbed; every cycle warns:

```
new table ORDERS present in the source but NOT captured — run
`a2h extract cdc1 --refresh-tables` to snapshot + adopt it
```

Run `a2h extract NAME --refresh-tables` (MCP: `refresh_tables: true`) to
**snapshot-load** the new tables' current rows into the trail (as INSERT records,
through the same idempotent apply) **and then adopt** them, so their live CDC
events are captured from the next cycle on — snapshot first, then changes, in that
order. PK-less new tables are reported and skipped (they can't be keyed). This is
the safe, explicit minimum; continuous auto-snapshot is a v2 item.

## Slot lifecycle (PostgreSQL) and teardown

A PostgreSQL logical slot **pins WAL on the source until it is dropped** — an
abandoned extract will fill the source's disk. Tear an extract down with:

```bash
a2h extract cdc1 --drop -c config.toml                 # drop slot + registry entry
a2h extract cdc1 --drop --purge-trail -c config.toml   # ...and delete the trail dir
```

`--drop` (MCP: `drop: true`) drops the slot so it stops pinning WAL and removes the
registry entry; add `--purge-trail` for a clean slate. Dropping keeps the trail by
default (the apply cursor lives in the registry, so a re-registered extract
re-derives it). Two safety properties:

- If the slot drop **fails** (e.g. the slot is still `active` on another
  connection), `--drop` **raises and keeps the registry entry** — a WAL-pinning
  slot never silently vanishes from `a2h extracts` while the command claims
  success. Resolve the slot (it may be active on another session) and retry.
- An already-absent slot is not an error (`dropped_slot=false`, entry removed
  cleanly).

MySQL and Oracle have no server-side object to drop; `--drop` just removes the
registry entry (and, with `--purge-trail`, the trail).

## Recovery cheatsheet

All of these are **fail-closed by design** — the extract/replicat refuses to
proceed rather than silently lose or corrupt data. Each error text is actionable;
this is the short version.

| Symptom | Cause | Fix |
|---|---|---|
| `CDC position file … exists but is empty` / `… holds a malformed cursor` | A crash truncated the binlog/LSN pos file mid-write. | Restore the file from backup, **or** delete it to deliberately re-anchor at the source's *current* position (accepting that changes made while it was gone are not captured). |
| `the trail was written against PostgreSQL coordinate epoch X but the source now reports Y` (`epoch.id` mismatch) | A PITR restore / timeline change, or the extract points at a different cluster reusing the trail dir. | Archive or remove the trail directory, then re-run. Already-applied records are safe (apply is idempotent; the apply cursor lives in the registry, not the trail). |
| `the trail's last position … orders AHEAD of the source's current stream end` | The source's coordinate space restarted: MySQL `RESET MASTER` / binlog basename change / failover to lower files, or a PostgreSQL rewind. | Same as above — archive/remove the trail dir and start a fresh epoch. |
| `unterminated line in a closed segment` / `corrupt/unparseable change record` at apply | Real trail corruption (a *terminated* bad line, not an in-flight tail — those self-heal). | Restore the segment from backup, or truncate it at the last good line, then re-run `replicat`. |
| `an UPDATE on … left primary-key column(s) … as an unchanged-TOAST datum` | A PostgreSQL non-key-changing UPDATE under `REPLICA IDENTITY DEFAULT` on a table with a TOASTed PK component (rare — btree limits usually keep PK values inline). | Set `REPLICA IDENTITY FULL` (or `USING INDEX <pk-index>`) on that table so the WAL carries the full key on every UPDATE. **The poisoned change is already decoded in the slot, so fixing the table alone does not unwedge the extract** — the slot re-peeks it every run. To unblock: **re-snapshot the table** (migrate it again, then recreate the slot), *or* advance/recreate the slot past the poisoned change (accepting the loss of changes up to that point). |
| `replicat … raises` with `dead_lettered` at the `poison_max_per_run` cap | Mass-poison circuit breaker: too many records failed at once. | Investigate the environment (wrong target? schema drift?). Once fixed, re-run — parked records are skipped and the backlog proceeds. |
| `replicat` refuses against a HeliosDB-Nano target | The Nano build is below the CDC-apply minimum (**3.58.5**). | Upgrade Nano to ≥ 3.58.5 (the tool refuses older builds with a clear message rather than a mid-apply failure). |

## Residual limitations to plan around

These are documented properties of the current spine, not bugs — design for them:

- **No transaction boundaries yet.** The trail is a flat per-row stream, so a
  source transaction that re-keys a parent row *and* re-points its children can
  arrive with the child re-points *after* the parent-key change. A target that
  enforces foreign keys **immediately** can reject the parent-key UPDATE. If your
  workload re-keys parent rows and your target enforces FKs, run the apply window
  with **deferred constraints** (`SET CONSTRAINTS ALL DEFERRED`, or declare the FKs
  `DEFERRABLE INITIALLY DEFERRED`) or drop/disable the FK for the CDC window and
  re-validate. Targets that don't enforce FKs are unaffected. (Full detail:
  [FK ordering across a re-keyed parent](../cdc.md#residual-limitation-fk-ordering-across-a-re-keyed-parent).)
- **Delete propagation is mode-aware.** Log-based sources (MySQL binlog,
  PostgreSQL logical) carry explicit `D` events, so reconcile is **off** by
  default. The Oracle **SCN-watermark** scan can't *see* deletes, so `replicat`
  reconciles them by a source/target key-set diff (**on** by default there) — an
  O(keys) pass. Override per run with `--reconcile-deletes` / `--no-deletes`, but
  forcing reconcile on for a log-based source can race a not-yet-applied key-move
  (see [Delete reconciliation](../cdc.md#delete-reconciliation-mode-aware-default)) —
  do it only when apply is fully caught up.
- **Oracle SCN specifics.** Capture is watermark-based (re-reads changed rows), not
  a continuous log reader. `ORA_ROWSCN` is **block-granular** unless the table was
  created `ROWDEPENDENCIES`, so capture may re-emit unchanged neighbouring rows
  (harmless — apply upserts by key). If no flashback/SCN function is permitted the
  engine falls back to a full re-capture each cycle (still correct, just heavier).
  Oracle emits no per-event position, so extract-start dedup and `source_pos`
  poison-dedup do not apply to Oracle trails.

## See also

- [CDC — the model](../cdc.md) — Extract → trail → Replicat, watermark/cursor
  semantics, idempotency, the keymove barrier, and the change-record format.
- [Configuration → `[cdc]`](configuration.md#cdc-change-data-capture-tuning) — every
  tunable and its default.
- [CLI reference → Change data capture](../reference/cli.md#change-data-capture) —
  full `extract` / `replicat` / `extracts` signatures.
- [Worked examples, scenario 5](examples.md#scenario-5) — runnable CDC cycles.
