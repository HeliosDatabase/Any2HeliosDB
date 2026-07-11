# Change data capture (CDC)

`a2h` ships a GoldenGate-style CDC spine: a symmetric **Extract → trail →
Replicat** pipeline where capture and apply advance on their own durable cursors.
v1 capture is Oracle **SCN-watermark**; the trail and replicat are
source-agnostic, so log-based sources and HeliosDB-as-source drop in later without
changing the apply side.

> **Edition support.** CDC apply (`replicat`) is **validated on HeliosDB-Full and
> HeliosDB-Lite**, and on **HeliosDB-Nano ≥ 3.58.5** (the tool refuses CDC apply
> against an older Nano with a clear error). Against any target, the resumable
> [migrate + resume](reference/cli.md#a2h-resume) path is also available for
> idempotent refreshes. See [HeliosDB compatibility](heliosdb-compatibility.md).

## The model

```
 Oracle source                    durable trail                 HeliosDB target
 ┌────────────┐   extract        ┌──────────────┐   replicat    ┌────────────┐
 │ ALL_* + PK │ ───────────────► │ trail.jsonl  │ ────────────► │  upsert /  │
 │ ORA_ROWSCN │  ChangeRecords   │ (append-only,│  ChangeRecords│  delete    │
 └────────────┘                  │  fsync'd)    │               └────────────┘
       ▲                         └──────────────┘                     ▲
   watermark (SCN)                                                apply cursor
   advances per capture                                          advances per apply
```

- **Extract** (`a2h extract NAME`) reads changed source rows and appends
  `ChangeRecord`s to the named trail, then advances that extract's **capture
  watermark**.
- **Trail** is a durable, append-only JSONL file (one per extract), fsync'd before
  an append returns.
- **Replicat** (`a2h replicat NAME`) reads from the trail starting at the extract's
  **apply cursor**, applies each record idempotently, and advances the cursor.

Capture and apply are decoupled: the watermark (highest SCN captured) and the
apply cursor (trail lines already applied) are tracked independently in a SQLite
registry (`<output_dir>/cdc.db`), so each survives process restarts and you can
run extract and replicat on different schedules or hosts (sharing the trail).

## The verbs

| Command | Purpose |
|---|---|
| `a2h extract NAME -c config.toml` | Capture source changes into the `NAME` trail; advance the watermark. |
| `a2h replicat NAME -c config.toml` | Apply the `NAME` trail to the target (idempotent); advance the apply cursor. |
| `a2h extracts -c config.toml` | List extracts with schema, table count, watermark, cursor, and state. |

`NAME` is yours to choose; the first `extract` registers it (capturing every table
in the configured schema) and subsequent runs refresh its table set.

### Example cycle

```
$ a2h extract cdc1 -c config.toml
extract cdc1: captured 8 change(s) (full snapshot); watermark=2547881

# ...rows change in Oracle...

$ a2h extract cdc1 -c config.toml
extract cdc1: captured 2 change(s) (incremental since SCN 2547881); watermark=2547990

$ a2h replicat cdc1 -c config.toml
replicat cdc1: applied 2 change(s) from 2 read; cursor=10

$ a2h extracts -c config.toml
  cdc1             schema=HR tables=2 watermark=2547990 cursor=10 state=applying
```

## Watermark & cursor semantics

### Capture watermark (SCN)

v1 capture uses Oracle's **System Change Number** via `ORA_ROWSCN`
([`cdc/sources/oracle_scn.py`](../src/any2heliosdb/cdc/sources/oracle_scn.py)):

- **First cycle** (watermark 0): a **full snapshot** — every row of every table
  with a primary key becomes an upsert record.
- **Subsequent cycles**: only rows where `ORA_ROWSCN > watermark` are re-emitted —
  an incremental capture.
- The new watermark is anchored **before** the scan (the current SCN at scan
  start), so commits that land *during* the scan are picked up next cycle rather
  than skipped.
- `ORA_ROWSCN` is **block-granular** unless the table was created with
  `ROWDEPENDENCIES`, so capture may re-emit unchanged neighbouring rows. That is
  harmless — the apply upserts by key, so a re-emitted unchanged row is a no-op.
- Tables **without a primary key** can't be keyed and are **skipped** (and listed
  in the command output).

If neither `dbms_flashback.get_system_change_number` nor
`timestamp_to_scn(systimestamp)` is permitted for the user, `current_scn()`
returns 0 and the engine falls back to a **full re-capture each cycle** — still
correct, because apply is idempotent.

### Apply cursor

The trail is a line-cursor: reading from cursor *N* returns every record after
line *N* and the new line count, which the replicat persists **only after a
successful apply**. Combined with idempotent upserts, this is at-least-once
delivery that is **effectively-once per row**.

#### Keymove barrier (per-record cursor around a PK move)

Every op except a **key-move** (a primary-key-changing UPDATE) is
target-state-idempotent: an upsert, a column-subset `update_columns`, and a
delete-by-key all converge no matter what window is replayed. A key-move is the
one exception — whether the old-key row is *"not yet moved"* or *"a different
logical row that later reused the freed key"* (an **impostor**) is **undecidable
from target state**, so replaying a key-move together with a neighbouring record
can converge to the wrong row.

The replicat therefore applies each key-move under a **durability barrier**: the
apply cursor is persisted **just before** and **just after** the key-move (each a
segment of exactly one record), with the non-key-move records around it batched
as before. A crash can then only ever force a key-move to **replay alone** — never
batched with the record before or after it — and a lone key-move replay is
convergent by construction (the move refreshes the old-key row in place, clears
the new-key slot, then updates the key columns; a replay past the move refreshes
the already-moved row instead). This closes the replay divergence structurally
rather than trying to *decide* the impostor case, which is impossible.

Key-moves are rare (they only occur on PK updates), so the extra cursor writes are
negligible; the barrier has **no configuration knob** because there is no tradeoff
to tune — a key-move must be isolated for correctness, always. A slice with no
key-move flushes the cursor exactly once at the end, byte-identical to the
previous per-slice behaviour.

## Idempotency

The whole point of the design is that **re-running a trail slice never corrupts
row state**:

- Apply buckets records by table and routes them through the target driver's
  `upsert` (full-image inserts/updates), `update_columns` (TOAST-partial and
  primary-key-changing updates), and `delete_keys` seams.
- On the `psycopg` driver, `upsert` issues
  `INSERT … VALUES (…) ON CONFLICT (key) DO UPDATE SET col = EXCLUDED.col, …` per
  row — updating the existing row in place. (Within one batch, the last record per
  key wins.)
- `update_columns` issues a keyed `UPDATE … SET <present columns> WHERE <key>`,
  touching only the columns present in the record. It backs both the unchanged-TOAST
  column-subset apply (omitted columns keep their stored value on every driver) and
  the in-place primary-key move (see below). If the keyed `UPDATE` matches no row,
  the apply falls back to inserting the present columns.
- Updating in place rather than DELETE-then-INSERT is **FK-safe**: deleting a
  parent row to re-insert it would trip an enforced foreign key on a target that
  checks immediately.
- On the `psycopg` driver these statements are built with **literal SQL** (escaped
  values, not bind parameters), because some editions mishandle `ON CONFLICT` and
  parameterized `WHERE` with binds.

So you can replay from any cursor position, re-run the same `replicat`, or
re-capture a full snapshot, and the target converges to the source.

## Delete reconciliation (mode-aware default)

`replicat` can remove target rows whose primary key is absent from the source by
diffing the two key-sets (`reconcile_deletes`). Whether that runs by **default**
depends on the source's capture **mode** — it is not a fixed flag:

| Source | Default | Why |
|---|---|---|
| Oracle **SCN-watermark** | **on** | A watermark scan can't *see* deleted rows, so the key-set diff is the only way deletes propagate. |
| MySQL **binlog**, PostgreSQL **logical** | **off** | These carry explicit `D` events, so reconcile is redundant — and it **races a key-move**: if the source has already moved a PK but the key-move is not yet applied, the diff sees the old key as "surplus" and deletes that row; the later key-move then finds neither old nor new key and inserts a **partial (TOAST-omitting) image**, silently losing the omitted column (no crash). |

Override the default explicitly on either mode:

- `a2h replicat NAME --no-deletes` — force reconcile **off** (e.g. an Oracle run
  where you don't want the full key-set pass).
- `a2h replicat NAME --reconcile-deletes` — force reconcile **on** for a log-based
  source. Do this only when you understand the lag race above; run it when the
  apply is fully caught up (no in-flight key-move), or a re-keyed row can be lost.

Over MCP the `replicat` tool's `reconcile_deletes` is the same tri-state: **omit**
it for the mode-aware default, or pass `true`/`false` to force. The default is
mode-derived (from `[source].dialect`), so there is no hardcoded reconcile knob.

## The change record

Each unit that flows source → trail → sink is a `ChangeRecord`
([`core/change_record.py`](../src/any2heliosdb/core/change_record.py)):

```json
{"op":"U","schema":"HR","table":"EMPLOYEES",
 "key":{"EMP_ID":{"__t__":"dec","v":"101"}},
 "after":{"EMP_ID":{"__t__":"dec","v":"101"},"NAME":"Ada",
          "HIRED":{"__t__":"ts","v":"2020-01-15T00:00:00"}},
 "scn":2547990,"commit_ts":""}
```

- **`op`** — `I` insert, `U` update, `D` delete. The Oracle SCN-watermark source
  emits only `U` (upserts); the log-based sources (MySQL binlog, PostgreSQL
  logical decoding) emit real `I`/`U`/`D`.
- **`key`** / **`after`** — the primary-key columns and the full after-image.
- **`before_key`** (optional) — the row's *old* primary key on a PK-changing
  UPDATE, so the replicat can move the row in place from its old key (see
  [Primary-key-changing UPDATEs](#primary-key-changing-updates)). It is present
  **only when the key moved**; the field is omitted from the JSON on every other
  record, so existing trails stay byte-for-byte identical and readers that predate
  the field simply ignore it.
- **`source_pos`** (optional) — the monotonic source position the change was
  captured at, encoded as one comparable integer per source (PostgreSQL LSN =
  `(hi << 32) | lo`; MySQL binlog = `(file-index << 48) | log_pos`; Oracle SCN =
  the SCN). It powers **extract-start dedup** (see below). Present only when the
  source supplies a per-event position; omitted otherwise, so legacy trails and
  older readers are unaffected.
- Oracle hands back `Decimal`, `datetime`, and `bytes` (LOB/RAW) values that plain
  JSON can't round-trip, so each is **type-tagged** on encode (`dec`, `ts`, `d`,
  `b64` base64 for bytes) and rebuilt on decode, preserving the exact type the
  target driver binds.

## v1 limitations

- **Deletes via reconciliation, not capture.** A watermark scan can't *see*
  deleted rows, so `replicat` reconciles them: it diffs the source's current
  primary-key set against the target's and removes the surplus. The default is
  **mode-aware** (see [Delete reconciliation](#delete-reconciliation-mode-aware-default)):
  **on** for the Oracle SCN source (which has no delete events), **off** for the
  log-based sources (which carry explicit `D` records). This is a full key-set
  pass (cost O(keys)); *incremental* delete capture via the change log
  (binlog / LogMiner) is the v2 roadmap. The trail format and the replicat also
  handle explicit `D` records for when a log-based source produces them.
- **SCN-watermark only.** Capture re-reads changed rows; it is not a continuous
  log reader. It is the guaranteed-portable Oracle "CDC" for shops without
  LogMiner / supplemental-logging access.
- **Block-granular ORA_ROWSCN** may over-capture (benign — apply is idempotent).
- **Primary key required** per table; PK-less tables are skipped.
- **Edition support** — apply is validated on Full and Lite, and on Nano ≥ 3.58.5
  (see the note above).

## Log-based capture — MySQL binlog (implemented)

For a MySQL source, capture reads the ROW-format **binlog** directly
(`mysql-replication`), producing real `I`/`U`/`D` change records — **including
deletes** — with the binlog coordinate (`<file>:<pos>`) as the cursor:

    pip install -e ".[mysql-cdc]"      # PyMySQL + mysql-replication

Prerequisites: `log_bin=ON`, `binlog_format=ROW`, `binlog_row_metadata=FULL` (the
source sets it best-effort when anchoring; otherwise set it server-side), and a
user with `REPLICATION SLAVE`/`REPLICATION CLIENT`. `extract` anchors at the
current position on first run, then captures incrementally. The binlog's own `D`
events do the deletes, so the replicat's key-set reconcile
[defaults **off** for a log-based source](#delete-reconciliation-mode-aware-default) —
you no longer need to pass `--no-deletes`. Battle-tested MySQL→HeliosDB
(insert + update + delete propagate through the log).

### Position-file durability & recovery

The binlog cursor `<file>:<pos>` is persisted to
`<output_dir>/trail/<name>/binlog.pos` **after** the batch is durably in the
trail. Two guarantees protect it:

- **Atomic write.** The file is replaced with a temp file + `fsync` +
  `os.replace` (the parent directory is fsync'd too), so a crash mid-write can
  never leave a torn/half-written coordinate — a reader always sees either the
  whole old value or the whole new one.
- **Fail-closed recovery.** On the next `extract`, a **missing** file means a
  genuinely fresh extract: anchor at the server's *current* coordinate and
  capture nothing yet. A file that **exists but is empty or malformed** (e.g.
  truncated by an older, interrupted writer) is treated as a corrupt cursor and
  `extract` **aborts with a clear error** instead of silently re-anchoring —
  re-anchoring from an empty cursor would skip every change between the last good
  position and "now" (unbounded data loss). Recover by restoring the file from
  backup, or delete it to *deliberately* re-anchor at the current position
  (accepting that changes made while it was gone are not captured).

### Extract-start dedup (no duplicate trail lines)

The trail is written **before** the source cursor advances (trail-first
durability), so a crash *between* the `trail.append` and the cursor write leaves
the batch durably in the trail while the durable cursor still points *before* it.
The next `extract` re-reads from that old cursor and re-delivers the same events —
which, without a guard, would append them a **second time**. A duplicate line is
not harmless for a key-move even with the [barrier](#keymove-barrier-per-record-cursor-around-a-pk-move):
a second key-move line would re-open the impostor case.

So each captured record is tagged with its `source_pos`, and on the next run the
extract reads just the **last complete** record already in the trail (a cheap tail
read, not a full scan) and **drops every captured event that orders `≤`** it before
appending. Trails without positions (legacy, or a source that cannot supply one —
e.g. the Oracle SCN-watermark path, whose upserts are idempotent anyway) skip
dedup unchanged. This applies to both log-based sources (MySQL binlog above,
PostgreSQL logical decoding below).

**Total per-record ordering.** All rows of one multi-row binlog event (and all
change lines sharing one LSN inside a PostgreSQL transaction) share a single
**base** coordinate. If dedup ordered on that base alone, a crash that persisted
only a *prefix* of the event's rows would make `≤ last` drop the never-trailed
remainder **forever**. So a `source_pos` is either a plain int (a singleton) or a
compound `[base, seq]` — `seq` being the record's ordinal within that base — and
dedup compares on the resulting total order. Because that order (row order within
an event, line order within a transaction) is stable across a re-read, a prefix
crash re-appends exactly the missing tail rows and nothing else. The wire format
stays backward compatible: a singleton keeps the bare int, and a legacy int `p`
orders identically to `[p, 0]`.

**Torn-tail self-heal.** `append` fsyncs once, after writing the whole batch, so a
crash can persist a *prefix ending mid-line* — a torn final fragment. Left alone,
the tail read would key dedup off that unparseable fragment (disabling dedup and
re-appending duplicates, including a re-opened key-move). So `extract` first
**truncates the torn fragment** (to the last complete line, then fsync) before
dedup. This is safe because a torn tail is by construction an in-flight append the
durable source cursor never advanced past, so re-capture + dedup restores those
events exactly once. On the apply side, `replicat`'s trail reader is also
torn-tail aware: it **stops before** an unterminated final line (an in-flight
append — neither applied nor wedging the reader) and **raises** on a *terminated
but corrupt mid-trail line* (real corruption, never silently skipped).

**Dedup window & coordinate epochs.** The crash-window overlap can only cover
`(resume position, trail tail]`, so dedup runs **only when the extract resumed
from a durable position** and the tail is at-or-ahead of it. A **freshly
anchored** extract (first run, or after the operator deliberately deleted the
position file) starts at the source's *current* coordinates and can never
re-read old events — nothing is dropped. Instead, a fresh anchor sanity-checks
that the surviving trail tail does not order *ahead of* the current stream end:
if it does, the source's **coordinate space restarted** underneath the trail
(`RESET MASTER` / `RESET BINARY LOGS`, a `log_bin` basename change, failover to
lower-numbered binlogs; for PostgreSQL, a restore/PITR rewind). Positions from
different epochs are not comparable, so the extract **fails closed** with
instructions to archive the trail directory and start a fresh epoch — archiving
is safe because apply is idempotent and the apply cursor lives in the registry,
not the trail.

For PostgreSQL the ordering check alone cannot catch a rewind whose new LSNs
*straddle* the stale tail, so the extract also persists the cluster's
**coordinate-space identity** (`system_identifier:timeline_id`, from
`pg_control_system()` / `pg_control_checkpoint()`) as `epoch.id` beside the
trail and fails closed when it changes — a PITR restore bumps the timeline, a
different cluster has a different system identifier. Where the control
functions are unavailable the identity probe degrades to the LSN-order check
alone.

**One extract process per name.** Trails have no inter-process lock: two
concurrent `a2h extract NAME` runs interleave buffered appends and race the
position file (and the torn-tail heal could truncate the other run's in-flight
line — surfacing later as a fail-closed corrupt-line error at apply). Run **one
extract process per extract name** (e.g. one cron entry); concurrent extracts of
*different* names are fine — every trail, position file, and cursor is
per-name.

## Log-based capture — PostgreSQL logical decoding (implemented)

For a PostgreSQL source, capture decodes the WAL through a logical replication
slot (the built-in `test_decoding` output plugin), producing real `I`/`U`/`D`
records — **including deletes** — with the slot LSN as the durable cursor. It
requires `wal_level = logical`, a role that may create a logical slot, and a
primary key on each table (the default `REPLICA IDENTITY` so UPDATE/DELETE carry
the key). Create the slot *before* the initial load — `a2h extract`'s first run
does so.

**Unchanged-TOAST columns.** When an UPDATE does not modify a large,
out-of-line (**TOASTed**) column, `test_decoding` emits the sentinel
`unchanged-toast-datum` in place of that column's value rather than re-sending the
datum. a2h **omits** such a column from the change record, and the replicat then
applies a **column-subset UPDATE** — a keyed SQL `UPDATE` that sets *only* the
columns actually present (`update_columns`) — so the target's stored value is
**left intact instead of being clobbered** with the literal marker string. Because
it is a true `UPDATE` (not a delete-then-reinsert), the omitted column survives on
**every** target driver — including the native Oracle back-end whose `upsert` is a
DELETE+INSERT that would otherwise NULL it (the psycopg `ON CONFLICT` and MySQL `ON
DUPLICATE KEY UPDATE` upserts merge on a conflict, but `update_columns` is what
keeps the subset image safe uniformly). If the row is absent (a replay that starts
mid-stream), the present columns are inserted instead — there is no prior value to
preserve. This is automatic and needs **no** server-side
setting; in particular you do *not* need `REPLICA IDENTITY FULL` for correctness
here. (A genuine text value that happens to equal the sentinel is sent
single-quoted by `test_decoding`, so it is never mistaken for the marker.)

**Exception — a TOASTed *primary-key* component.** If a key column is itself a
large, out-of-line value and an UPDATE leaves it unchanged, `test_decoding` omits
it too — but a PK component may **never** be dropped, or the row would be keyed on
`NULL` (a guaranteed apply failure / silent `WHERE pk = NULL`). The parser
recovers the omitted key component from the UPDATE's **pre-image** (`old-key`) when
one is present (a PK-changing UPDATE, or any UPDATE under `REPLICA IDENTITY FULL`).
When it is *not* recoverable — a non-key-changing UPDATE under `REPLICA IDENTITY
DEFAULT`, which emits no pre-image — the extract **fails closed** with an
actionable error asking you to set `REPLICA IDENTITY FULL` (or `USING INDEX
<pk-index>`) on that table so the WAL carries the full key on every UPDATE. Failing
closed beats corrupting the row with a `NULL` key. (This is a narrow edge: btree
index limits keep most PK values inline, so they are rarely out-of-line TOASTed.)

## Primary-key-changing UPDATEs

When an UPDATE changes a row's primary key, both log-based sources record the
row's **old** key in the change record's `before_key` (MySQL from the binlog
before-image; PostgreSQL from `test_decoding`'s `old-key`/pre-image). The replicat
moves the row using only `update_columns` / `delete_keys` / an insert-of-provided —
never relying on a merge-upsert — so that re-applying the same trail slice (once,
twice, or resumed mid-stream) always converges to the same state with no unique
violation and no lost TOAST value, on **every** driver. The steps are:

1. **Refresh + probe the old-key row.** `update_columns` sets the provided non-key
   columns `WHERE` the *old* key. This preserves an omitted (unchanged-TOAST)
   column and can never collide; its matched-row count tells us whether the
   old-key row is still present (`update_columns` reports rows *matched*, so a
   pure-PK change whose other columns are unchanged is still detected).
2. **If the old-key row is present:** delete any row already sitting at the *new*
   key — a stale leftover from an earlier partial/complete replay of this same
   move (the source cannot hold a live row at the new key at this point in the
   stream, so the delete is convergent, and it is never the old-key/parent row) —
   then move the row by updating its **key columns** `WHERE` the old key. Clearing
   the new-key slot first is what makes the move immune to a unique violation on
   replay.
3. **If the old-key row is absent** (a replay that runs after the move already
   happened): refresh the already-moved row at the *new* key instead, again
   preserving an omitted TOAST value (an `UPDATE` never deletes the row that holds
   the only copy). Only when *that* also matches nothing — the row is absent
   everywhere — are the provided columns inserted, in which case an omitted TOAST
   value is genuinely unrecoverable.

This has two structural advantages over a delete-then-reinsert of the whole row:
it **preserves an unchanged-TOAST column across the move** (the TOASTed value only
lived on the old-key row), and it **never deletes the parent row**, so it does not
trip an enforced foreign key on a target that checks constraints immediately (the
hazard the psycopg driver's `upsert` comments call out). Non-PK-changing UPDATEs
carry no `before_key` and remain a plain keyed upsert (full image) or column-subset
`UPDATE` (TOAST-partial image).

These per-record steps are convergent **only because the replicat never replays a
key-move alongside another record** — the crash-replay window around a key-move is
narrowed to the key-move alone by the
[keymove barrier](#keymove-barrier-per-record-cursor-around-a-pk-move). Together
they make a key-move fully replay-safe on every driver.

### Residual limitation: FK ordering across a re-keyed parent

The in-place `UPDATE` removes the *delete-the-parent* hazard, but a
foreign-key-enforcing target can still reject a **parent-key** `UPDATE` on its own:
the source may have re-keyed a parent and re-pointed its children **in one
transaction**, but the trail is a flat per-row stream and the replicat has **no
transaction boundaries yet** (a v2 backlog item), so the child re-points can arrive
*after* the parent-key change. An immediate-checking FK then sees a moment where
children still reference the old parent key. If your target enforces foreign keys
and your workload re-keys parent rows, either run the apply inside a window with
**deferred constraints** (`SET CONSTRAINTS ALL DEFERRED`, or declare the FKs
`DEFERRABLE INITIALLY DEFERRED`) or drop/disable the FK for the CDC window
(the `--no-fk` load posture) and re-validate afterwards. Targets that do not
enforce FKs (e.g. HeliosDB-Lite in parse-only mode) are unaffected.

## v2 roadmap

Built on the same Extract → trail → Replicat spine (trail + apply unchanged):

- **More log-based sources** — Oracle LogMiner and SQL Server CDC (MySQL binlog
  and the heterogeneous **migrate-back** targets are already done — see the README).
