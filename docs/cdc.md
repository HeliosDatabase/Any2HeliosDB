# Change data capture (CDC)

`a2h` ships a GoldenGate-style CDC spine: a symmetric **Extract → trail →
Replicat** pipeline where capture and apply advance on their own durable cursors.
v1 capture is Oracle **SCN-watermark**; the trail and replicat are
source-agnostic, so log-based sources and HeliosDB-as-source drop in later without
changing the apply side.

> **Edition support.** CDC apply (`replicat`) is **validated on HeliosDB-Full and
> HeliosDB-Lite**, and on **HeliosDB-Nano ≥ 3.58.3** (the tool refuses CDC apply
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

## Idempotency

The whole point of the design is that **re-running a trail slice never corrupts
row state**:

- Apply buckets records by table and routes them through the target driver's
  `upsert` (for inserts/updates) and `delete_keys` seams.
- On the `psycopg` driver, `upsert` issues
  `INSERT … VALUES (…) ON CONFLICT (key) DO UPDATE SET col = EXCLUDED.col, …` per
  row — updating the existing row in place. (Within one batch, the last record per
  key wins.)
- Updating in place rather than DELETE-then-INSERT is **FK-safe**: deleting a
  parent row to re-insert it would trip an enforced foreign key on a target that
  checks immediately.
- On the `psycopg` driver these statements are built with **literal SQL** (escaped
  values, not bind parameters), because some editions mishandle `ON CONFLICT` and
  parameterized `WHERE` with binds.

So you can replay from any cursor position, re-run the same `replicat`, or
re-capture a full snapshot, and the target converges to the source.

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

- **`op`** — `I` insert, `U` update, `D` delete. v1 SCN-watermark capture emits
  only `U` (upserts); `I`/`D` are reserved for log-based v2 sources.
- **`key`** / **`after`** — the primary-key columns and the full after-image.
- Oracle hands back `Decimal`, `datetime`, and `bytes` (LOB/RAW) values that plain
  JSON can't round-trip, so each is **type-tagged** on encode (`dec`, `ts`, `d`,
  `b64` base64 for bytes) and rebuilt on decode, preserving the exact type the
  target driver binds.

## v1 limitations

- **Deletes via reconciliation, not capture.** A watermark scan can't *see*
  deleted rows, so `replicat` reconciles them: it diffs the source's current
  primary-key set against the target's and removes the surplus (on by default;
  `--no-deletes` to skip, `--reconcile-deletes` to force). This is a full key-set
  pass (cost O(keys)); *incremental* delete capture via the change log
  (binlog / LogMiner) is the v2 roadmap. The trail format and the replicat also
  handle explicit `D` records for when a log-based source produces them.
- **SCN-watermark only.** Capture re-reads changed rows; it is not a continuous
  log reader. It is the guaranteed-portable Oracle "CDC" for shops without
  LogMiner / supplemental-logging access.
- **Block-granular ORA_ROWSCN** may over-capture (benign — apply is idempotent).
- **Primary key required** per table; PK-less tables are skipped.
- **Edition support** — apply is validated on Full and Lite, and on Nano ≥ 3.58.3
  (see the note above).

## Log-based capture — MySQL binlog (implemented)

For a MySQL source, capture reads the ROW-format **binlog** directly
(`mysql-replication`), producing real `I`/`U`/`D` change records — **including
deletes** — with the binlog coordinate (`<file>:<pos>`) as the cursor:

    pip install -e ".[mysql-cdc]"      # PyMySQL + mysql-replication

Prerequisites: `log_bin=ON`, `binlog_format=ROW`, `binlog_row_metadata=FULL` (the
source sets it best-effort when anchoring; otherwise set it server-side), and a
user with `REPLICATION SLAVE`/`REPLICATION CLIENT`. `extract` anchors at the
current position on first run, then captures incrementally; apply the binlog's
own deletes with `replicat --no-deletes`. Battle-tested MySQL→HeliosDB
(insert + update + delete propagate through the log).

## v2 roadmap

Built on the same Extract → trail → Replicat spine (trail + apply unchanged):

- **More log-based sources** — Oracle LogMiner and SQL Server CDC (MySQL binlog
  and the heterogeneous **migrate-back** targets are already done — see the README).
