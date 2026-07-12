# Any2HeliosDB MCP server

The MCP (Model Context Protocol) server exposes the `a2h` migration toolkit as
MCP **tools**, so an AI agent (Claude, or any MCP-aware client) can drive a
migration remotely — assess, migrate, validate, resume, run CDC — over the
network, with **Bearer-token authentication** and **role-based access control**.

Every tool wraps an Any2HeliosDB *engine* function directly (it does **not**
shell out to the CLI) and returns **structured JSON**, not human text, so an
agent can branch on the result (`failed_chunks`, `passed`, row counts, …).

## Quick start

```bash
# 1. install the extra (the official MCP SDK on Python >= 3.10; on 3.9 the
#    server uses a built-in, wire-compatible JSON-RPC endpoint — no extra deps)
pip install 'any2heliosdb[mcp]'

# 2. configure tokens out-of-band (token:role pairs)
export A2H_MCP_TOKENS='ci-token:operator,agent-token:admin,readonly-token:viewer'

# 3. serve over HTTP for remote agents
a2h mcp serve --transport http --host 0.0.0.0 --port 8080
#   -> http://<host>:8080/mcp
```

Local (single-process) use over stdio, for an agent that launches the server as
a subprocess:

```bash
a2h mcp serve --transport stdio --stdio-role operator
```

## Transport / SDK

* **HTTP** (default) — a streamable JSON-RPC-2.0 endpoint at `POST /mcp` (the
  streamable-HTTP transport; `GET /mcp` opens the SSE server→client channel).
  Remote agents connect here. `GET /healthz` is an unauthenticated liveness
  probe.
* **stdio** — newline-delimited JSON-RPC over stdin/stdout for a local agent.

The server uses the official **`mcp` Python SDK (FastMCP)** when it is
importable. That SDK requires **Python ≥ 3.10**; on older runtimes (this repo's
CI runs 3.9) the server falls back to a **minimal, wire-compatible JSON-RPC-2.0
implementation** built on the standard library — same `initialize` /
`tools/list` / `tools/call` contract, no third-party dependency required to run
it. `a2h mcp serve` prints which path it took (`sdk_path: fastmcp` or
`builtin-jsonrpc`).

## Authentication (Bearer token)

Every HTTP request **must** carry an `Authorization: Bearer <token>` header. A
missing or malformed header, or an unknown token, is rejected with **HTTP 401**
*before* any method runs. The raw token is never logged — only a short
non-secret fingerprint (`tok_xxxxxxxx`).

Tokens are configured **out of band** (never in the migration config the tools
operate on), from two sources that are merged (the file wins on a clash, so an
on-disk rotation overrides a stale environment export):

| Source | Format |
| --- | --- |
| `A2H_MCP_TOKENS` env var (or `--tokens`) | `token:role,token:role` (comma/space separated) |
| `A2H_MCP_TOKENS_FILE` env var (or `--tokens-file`) | one `token:role` per line; `#` comments + blanks ignored |

```
# tokens.txt
# rotate weekly
agent-7f3a9c:admin
ci-runner:operator
dashboard:viewer
```

```bash
a2h mcp serve --transport http --tokens-file ./tokens.txt
```

> The HTTP transport refuses to start with zero tokens configured (it would be
> an open relay). stdio is a trusted local launch and is granted a role via
> `--stdio-role` (default `admin`).

### Generating a token file — `a2h mcp auth`

Rather than hand-write the file (and risk leaking a token through your shell
history), let a2h generate a cryptographically-strong token straight into a
private file:

```bash
# generate an admin token in the default file (~/.config/a2h/mcp-tokens), mode 0600
a2h mcp auth --role admin

# pick a role + path; --show also prints the raw token (otherwise it stays in the file)
a2h mcp auth --role operator --file ./tokens.txt --show

# add another token to the same file (append by default), or --rotate to replace all
a2h mcp auth --role viewer
```

The file is created **`0600`** (owner-only) and contains the `token:role` lines
`--tokens-file` reads — keeping the secret off the command line and out of the
project config. The default path is `$A2H_MCP_TOKENS_FILE` if set, else
`~/.config/a2h/mcp-tokens`. The raw token lives only in the file; a client takes
it as the first `:`-field of a line:

```bash
a2h mcp serve --tokens-file ~/.config/a2h/mcp-tokens            # server reads it
TOKEN=$(cut -d: -f1 ~/.config/a2h/mcp-tokens | head -1)         # client reads it
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/mcp -d '{...}'
```

Keep the file out of version control — it grants the role it encodes.

## RBAC (roles)

Roles form a strict hierarchy — a higher role inherits every permission below
it: **`viewer` < `operator` < `admin`**. Each tool declares the *minimum* role
required. `tools/list` returns only the tools the caller's role may call, and a
`tools/call` to a tool above the caller's role returns a **403-equivalent MCP
error** (JSON-RPC code `-32001`, `data.status = 403`).

| Tool | Min role | Wraps |
| --- | --- | --- |
| `doctor` | viewer | local environment / driver check |
| `smoke_test` | viewer | `config.wizard.smoke_test` |
| `assess` | viewer | `assess.report.build_report` |
| `status` | viewer | `core.manifest.Manifest.summary` |
| `extracts` | viewer | `cdc.engine.list_extracts` |
| `test` | viewer | `validate.structure.run_test` |
| `test_count` | viewer | `validate.counts.run_test_count` |
| `test_data` | viewer | `validate.data.run_test_data` |
| `test_index` | viewer | `validate.data.run_test_index` |
| `export` | viewer | `core.export.build_ddl` (+ `plsql.procedural.render_review`) |
| `list_config` | viewer | resolved config, passwords redacted |
| `validate_config` | viewer | config loads + runtime objects build (no I/O) |
| `migrate` | operator | `core.orchestrator.migrate` |
| `load` | operator | `core.orchestrator.migrate` |
| `resume` | operator | `core.orchestrator.migrate` (resume) |
| `extract` | admin | `cdc.engine.run_extract` |
| `replicat` | admin | `cdc.engine.run_replicat` |
| `wizard` | admin | headless config write (`save_config`) |

Role → permitted-tool matrix:

| Role | Permitted tools |
| --- | --- |
| **viewer** | doctor, smoke_test, assess, status, extracts, test, test_count, test_data, test_index, export, list_config, validate_config |
| **operator** | *(all viewer)* + migrate, load, resume |
| **admin** | *(all operator)* + extract, replicat, wizard |

## Specifying the config

Every tool takes **either** a `config` path on the server **or** an inline
config (the same `[source]` / `[target]` / `[options]` blocks, plus optional
`data_type` / `modify_type`). Inline config is fed through the exact same
`load_config` the CLI uses, so parsing and validation never drift.

```jsonc
// tools/call arguments — by path
{ "config": "/etc/a2h/prod.toml" }

// tools/call arguments — inline
{
  "source":  { "dialect": "oracle", "host": "ora1", "user": "hr",
               "password_env": "ORA_PW", "service_name": "XEPDB1", "schema": "HR" },
  "target":  { "driver": "psycopg", "host": "helios1", "dbname": "hr" },
  "options": { "output_dir": "/var/a2h/hr", "parallelism": 8 }
}
```

Tool-specific arguments: `migrate`/`load` accept `parallelism`, `batch_size`,
`drop_existing`; `test_data` accepts `sample`; `extract`/`replicat` require
`name` (`replicat` also accepts `reconcile_deletes`); `wizard` accepts `output`.
`export` and `test_index` take only the config (path or inline). `export`
returns the entire DDL (and `.review.sql` companion) as text in one
response — for very large schemas (thousands of tables) expect a
multi-megabyte JSON-RPC body; there is deliberately no truncation. Note
`chunks_per_worker`, `native_call_timeout_ms`, and the source/target
`connect_timeout` are set via the inline `options`/`source`/`target` blocks (the
same keys `config.toml` uses); `migrate` honours whatever the resolved config
carries.

Unlike the CLI `a2h export` (which writes `schema.sql` + `schema.review.sql`), the
`export` tool **returns the DDL text** so an agent can consume it directly:

```jsonc
// export result (structuredContent)
{ "ok": true,
  "ddl": "CREATE TABLE employees (...);\n\nCREATE SEQUENCE ...",
  "review_sql": "-- a2h review file: ...\n"   // null when nothing procedural to port
}
```

## Result shape

`tools/call` returns the MCP standard `content` (a text block with the JSON) and
also `structuredContent` (the same object, for structured-output clients).
Long/destructive operations return their full stats and **reflect failure** in
the result:

```jsonc
// migrate result (structuredContent)
{
  "ok": false,            // false when failed_chunks > 0 — a partial load is NOT success
  "tables": 12,
  "total_rows": 480321,
  "rows": { "HR.EMP": 107, "HR.DEPT": 27, "...": 0 },
  "load_mode": "copy",
  "failed_chunks": 1,
  "incomplete": true,     // run `status` then `resume`
  "warnings": ["fk on HR.EMP: ..."],
  "run_id": "run_a1b2c3d4e5"
}
```

Validation tools set `ok` to the `passed` flag and include every finding:

```jsonc
{ "ok": false,
  "result": { "validation_type": "TEST_COUNT", "passed": false,
              "errors": [{"severity": "blocker", "table": "HR.EMP",
                          "message": "DataMismatch: row count differs ..."}],
              "metrics": {"tables_checked": 12, "tables_matched": 11} } }
```

A tool that *runs* but fails (e.g. the target is unreachable) returns a normal
result with `isError: true` and `ok: false` (distinct from a 401/403, which are
auth/RBAC outcomes, not tool outcomes).

## Wire examples (curl)

```bash
TOK=agent-token; U=http://127.0.0.1:8080/mcp

# list the tools this token's role may call
curl -s -X POST $U -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# assess a schema (inline config)
curl -s -X POST $U -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"assess",
       "arguments":{"config":"/etc/a2h/prod.toml"}}}'

# missing token -> 401
curl -i -s -X POST $U -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/list"}' | head -1
```

## Connecting from Claude Code

Add the server (HTTP transport, with the Bearer token in a header):

```bash
claude mcp add --transport http any2heliosdb http://your-host:8080/mcp \
  --header "Authorization: Bearer agent-token"
```

For a local stdio launch:

```bash
claude mcp add any2heliosdb -- a2h mcp serve --transport stdio --stdio-role operator
```

## Security notes

* Tokens are bearer credentials — serve HTTP behind TLS (a reverse proxy) on any
  untrusted network; the token is the only thing standing between a caller and
  `migrate`/`extract`.
* Scope tokens to the least role that works (a CI runner needs `operator`, a
  monitoring dashboard only `viewer`).
* `list_config` redacts literal passwords; prefer `password_env` so secrets are
  never in a config the server can echo at all.
* Rotate via the tokens file (it overrides the env var) without restarting your
  deployment tooling.
