# Troubleshooting

This page covers common errors, how the tool reports incompatibilities, and a
per-edition summary of the minimum HeliosDB builds a2h is validated against.

## How a2h adapts to the target

When a migration hits something the target can't accept, the tool follows one
rule:

> **Work around the gap minimally, and adapt to what the target actually
> supports** — so the translation layer stays thin and the same `migrate`
> behaves correctly across editions.

Two practical consequences when you're debugging:

1. **A validation failure can be real target divergence, surfaced — not a tool
   bug.** For example, `a2h test-data` flags a content mismatch when a target
   stores a value differently from the source. The tool is doing its job —
   catching divergence — so the first step is to check the target build against
   the minimums below.
2. **The capability probe adapts at runtime.** The tool asks the live server what
   it accepts and only translates what *this* target can't take. So the same
   `migrate` behaves correctly across Nano / Lite / Full and stock PostgreSQL
   without edition flags. See
   [HeliosDB compatibility](heliosdb-compatibility.md) for how the probe and
   graceful degradation work.

## Common errors

### `config not found: config.toml (run `a2h wizard`)`

No config at the path. Run `a2h wizard`, or pass `-c path/to/config.toml`.

### Source connect fails (`Oracle connect failed: …`)

- Check `host`/`port` and that exactly one of `service_name` or `sid` is set in
  `[source]`.
- Confirm the password env var is exported: `echo $ORACLE_PW`. If `password_env`
  is set but the variable is empty, you'll get an auth failure.
- The user needs read on the `ALL_*` data-dictionary views and `SELECT` on the
  schema's tables.

### Target connect fails / `fe_sendauth: no password supplied`

If a trust-mode connect is rejected, supply *any* password via `password_env`, or
upgrade to a current build. For all editions, verify the PG-wire `port`,
`dbname`, and `user`.

### `no manifest at … (run `a2h migrate` first)` from `status`/`resume`

`status` and `resume` read the run manifest created by `migrate`. Run `a2h
migrate` first (with `[options]` present so the resumable loader is used), or check
that `output_dir` matches the migrate that created it.

### COPY fails, then INSERT retry (a warning, not a failure)

In the sequential path, a COPY protocol error can desync the connection; the tool
reconnects and retries the table via INSERT, logging a warning. In the chunked
loader, a chunk that fails under parallel contention is simply re-run by the
**serial-retry** pass. Both are self-healing — chunks are idempotent.

### `test-data` reports a BLOB / `bytea` row checksum mismatch

The target is storing binary differently from the source. This is the canonical
case where validation catches target divergence — confirm the target build meets
the per-edition minimum below. **HeliosDB-Lite** stores BLOBs intact; a mismatch
there points elsewhere (check the type override or the source value).

### `CREATE SEQUENCE … INCREMENT BY` warning

On a build that does not yet implement `CREATE SEQUENCE … INCREMENT BY/START
WITH`, the tool records a warning and continues; the table data still migrates.
Recreate sequences by hand if you need their exact start/increment, or migrate to
stock PostgreSQL (which creates them natively).

### CDC `replicat` fails or is skipped

CDC apply is edition-specific:

- **Full**: validated.
- **Lite**: validated on a current build.
- **Nano**: validated on **Nano ≥ 3.58.3**; the tool refuses CDC apply against an
  older Nano with a clear error rather than a mid-apply failure.

### `native` driver: `DPY-3010` / TNS handshake rejected

The experimental Oracle-wire driver's live parity test is blocked on a HeliosDB
Oracle-listener TNS-version handshake — `oracledb` thin mode rejects a `VSNNUM`
below its minimum. Use `driver = "psycopg"` (the validated default) for
production work.

## Minimum HeliosDB build by edition

See [HeliosDB compatibility](heliosdb-compatibility.md) for the full table and the
capability-probe behavior. In short:

| Edition | Minimum | Bulk load | CDC apply |
|---|---|---|---|
| **HeliosDB-Nano** | 3.58.3 | INSERT (no COPY) | ✅ (≥ 3.58.3) |
| **HeliosDB-Lite** | 2.0 | COPY | ✅ |
| **HeliosDB-Full** | current `main` build | COPY | ✅ |
| **Stock PostgreSQL** | 14+ | COPY | ✅ |

On a build older than the minimum, a2h still degrades gracefully via the
capability probe (INSERT instead of COPY, a serial-retry pass instead of parallel
transactions, a warning instead of native `CREATE SEQUENCE`) and reports any
work-around it had to apply. **Nano** returns `bytea` as a PG hex string
(`'\x…'`); a2h's `test-data` normalizes it, so validation passes. **Lite** stores
BLOBs intact.

## Still stuck?

- Re-run `a2h doctor` to confirm the right drivers are installed.
- Run `a2h assess` to see the inventory + type provenance the tool derived.
- Check `<output_dir>/manifest.db` exists and `a2h status` shows the run.
- Confirm your HeliosDB build meets the per-edition minimum above — many "tool"
  symptoms are target gaps already closed in a newer build.
