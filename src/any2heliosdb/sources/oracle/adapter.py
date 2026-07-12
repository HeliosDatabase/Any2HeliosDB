"""Oracle source adapter (python-oracledb).

Connects in **thin mode by default** — pure-Python, no Oracle Instant Client, no
external dependencies (the preferred, lightweight path). **Thick mode** (Instant
Client) is available opt-in (``[source] thick = true``) for the one case thin mode
can't serve: servers that mandate **Native Network Encryption / Data Integrity**
(thin mode raises DPY-3001). ``[source] sysdba = true`` connects with SYSDBA (for
the SYS user).

Introspects the Oracle data dictionary (``ALL_*`` views, owner-filtered) into the
canonical IR and streams table data for the loader. LOBs are fetched as
``str``/``bytes`` (``fetch_lobs=False``) so the load path never juggles locators.

Columns carry both a resolved default ``data_type`` and the verbatim
``source_type`` so the emit layer can re-resolve with user DATA_TYPE/MODIFY_TYPE
overrides.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ...constants import SourceDialect
from ...core.catalog_model import (
    Column,
    Constraint,
    ConstraintKind,
    ForeignKey,
    Index,
    IndexColumn,
    Partition,
    PartitionInfo,
    PartitionType,
    PrimaryKey,
    Routine,
    RoutineKind,
    Schema,
    Sequence as SequenceObj,
    Table,
    TableOptions,
    Trigger,
    View,
)
from ...errors import IntrospectionError, SourceConnectionError
from ..base import SourceAdapter, SourceDsn
from ...typemap.defaults import map_oracle_type

_LOG = logging.getLogger("any2heliosdb.sources.oracle")

_thick_inited = False


def _init_thick_mode(client_dir: Optional[str]) -> None:
    """Enable python-oracledb thick mode (Oracle Instant Client), once per process.

    Thick mode is required to reach Oracle servers that mandate Native Network
    Encryption / Data Integrity (thin mode raises DPY-3001). The Instant Client
    must be installed; ``client_dir`` points at its lib dir, else it is found via
    PATH / LD_LIBRARY_PATH. Raises a clear :class:`SourceConnectionError` if the
    client can't be loaded.
    """
    global _thick_inited
    if _thick_inited:
        return
    import oracledb
    try:
        oracledb.init_oracle_client(lib_dir=client_dir or None)
        _thick_inited = True
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "already" in msg.lower():  # initialized elsewhere — fine
            _thick_inited = True
            return
        raise SourceConnectionError(
            "could not enable Oracle thick mode (Instant Client): {}. Install the "
            "Oracle Instant Client and set [source] client_dir to its lib directory "
            "(or add it to LD_LIBRARY_PATH). Thick mode is required for servers that "
            "mandate Native Network Encryption.".format(msg)) from e


def _oracle_connect_hint(exc: Exception, dsn: SourceDsn) -> str:
    """Wrap a connect failure with actionable guidance for the common cases."""
    msg = str(exc)
    base = "Oracle connect failed: {}".format(msg)
    if "DPY-3001" in msg and not dsn.thick:
        return (base + "\nHINT: this server mandates Native Network Encryption, which "
                "needs python-oracledb THICK mode. Set [source] thick = true (and "
                "install the Oracle Instant Client; optionally client_dir = "
                "\"/path/to/instantclient\").")
    if "ORA-28009" in msg or ("SYS" in (dsn.user or "").upper() and "as SYSDBA" in msg):
        return (base + "\nHINT: connecting as SYS requires SYSDBA — set [source] "
                "sysdba = true, or connect as a regular schema user instead.")
    return base


def _reconstruct_source_type(
    data_type: str,
    data_length: Optional[int],
    data_precision: Optional[int],
    data_scale: Optional[int],
    char_length: Optional[int],
) -> str:
    dt = data_type.upper()
    if dt in ("VARCHAR2", "NVARCHAR2", "VARCHAR", "CHAR", "NCHAR"):
        n = char_length or data_length or 1
        return "{}({})".format(dt, n)
    if dt == "NUMBER":
        if data_precision is not None:
            if data_scale:
                return "NUMBER({},{})".format(data_precision, data_scale)
            return "NUMBER({})".format(data_precision)
        return "NUMBER"
    if dt == "RAW":
        return "RAW({})".format(data_length or 1)
    return dt  # DATE, TIMESTAMP(6) [WITH TIME ZONE], CLOB, BLOB, etc. are self-describing


def _oid(name: str) -> str:
    """Quote one Oracle identifier, doubling any embedded double-quote.

    A legally double-quoted Oracle name may contain a literal ``"`` (stored as
    ``""``). Interpolating it raw would break or alter the SQL, so every
    identifier that reaches a query string goes through here.
    """
    return '"{}"'.format(str(name).replace('"', '""'))


def quote_oracle(owner: str, name: str) -> str:
    return "{}.{}".format(_oid(owner), _oid(name))


# A column's NOT NULL is implemented in Oracle as a system-generated CHECK with
# the exact single-column form `"COL" IS NOT NULL`; that one is already modeled on
# the column, so it's the ONLY check we drop. A real multi-term CHECK that merely
# ends with IS NOT NULL (e.g. `email IS NULL OR phone IS NOT NULL`) is preserved.
_GENERATED_NOTNULL = re.compile(r'^"?([A-Za-z0-9_$#]+)"?\s+IS\s+NOT\s+NULL$', re.IGNORECASE)

# An `AS OF SCN` read fails if the snapshot predates a table's DDL (ORA-01466
# "table definition has changed") or falls outside the undo window; on these the
# adapter falls back to a current read for that query (read-consistency for that
# table is then unavailable — fine for the common stable/quiesced source).
_FLASHBACK_ERR_CODES = ("ORA-01466", "ORA-08180", "ORA-08181", "ORA-30052")


def _is_flashback_error(exc: object) -> bool:
    s = str(exc)
    return any(code in s for code in _FLASHBACK_ERR_CODES)


class OracleAdapter(SourceAdapter):
    dialect = SourceDialect.ORACLE

    def __init__(self, dsn: SourceDsn) -> None:
        super().__init__(dsn)
        self._conn: Any = None
        self._snapshot_scn: Optional[int] = None  # set via use_snapshot() for AS OF SCN reads

    def connect(self) -> None:
        import os
        import oracledb  # lazy

        oracledb.defaults.fetch_lobs = False  # CLOB->str, BLOB->bytes
        # Thick mode (Oracle Instant Client). Required for servers that mandate
        # Native Network Encryption / Data Integrity — thin mode raises DPY-3001.
        if self.dsn.thick or os.environ.get("A2H_ORACLE_THICK"):
            _init_thick_mode(self.dsn.client_dir or os.environ.get("ORACLE_CLIENT_DIR"))

        if self.dsn.service_name:
            conn_dsn = "{}:{}/{}".format(self.dsn.host, self.dsn.port, self.dsn.service_name)
        elif self.dsn.sid:
            conn_dsn = oracledb.makedsn(self.dsn.host, self.dsn.port, sid=self.dsn.sid)
        else:
            conn_dsn = "{}:{}".format(self.dsn.host, self.dsn.port)
        kw: Dict[str, Any] = {"user": self.dsn.user, "password": self.dsn.password, "dsn": conn_dsn}
        # Bound the connection-establishment wait (oracledb's own parameter) so a
        # firewalled/unreachable source fails fast instead of hanging assess/migrate.
        if self.dsn.connect_timeout:
            kw["tcp_connect_timeout"] = self.dsn.connect_timeout
        if self.dsn.sysdba:
            kw["mode"] = oracledb.AUTH_MODE_SYSDBA  # required for the SYS user
        try:
            self._conn = oracledb.connect(**kw)
        except Exception as e:  # noqa: BLE001
            raise SourceConnectionError(_oracle_connect_hint(e, self.dsn)) from e

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            raise SourceConnectionError("Oracle adapter not connected")
        return self._conn

    def _q1(self, sql: str, **binds: object) -> Optional[Tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, binds)
            return cur.fetchone()

    def _qall(self, sql: str, **binds: object) -> List[Tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, binds)
            return cur.fetchall()

    def _q1_consistent(self, build):
        """Run a single-row read that uses the snapshot, falling back to a current
        read on a flashback error. ``build(as_of_clause)`` returns the SQL given an
        ``AS OF SCN n`` (or empty) clause."""
        try:
            return self._q1(build(self._as_of()))
        except Exception as e:  # noqa: BLE001
            if self._snapshot_scn and _is_flashback_error(e):
                _LOG.warning("AS OF SCN %s read failed (%s); falling back to a current "
                             "read — read-consistency is not guaranteed for this object",
                             self._snapshot_scn, str(e).splitlines()[0][:90])
                return self._q1(build(""))
            raise

    def server_version(self) -> str:
        return str(self.conn.version)

    def default_schema(self) -> str:
        if self.dsn.schema:
            return self.dsn.schema.upper()
        row = self._q1("SELECT USER FROM dual")
        return str(row[0]) if row else self.dsn.user.upper()

    def current_scn(self) -> int:
        """Best-effort current system change number, for the CDC watermark.

        Tries the flashback package, then the always-grantable TIMESTAMP_TO_SCN.
        Returns 0 if neither is permitted (the caller then falls back to a
        full re-capture each cycle, still correct via idempotent upserts).
        """
        for sql in ("SELECT dbms_flashback.get_system_change_number FROM dual",
                    "SELECT timestamp_to_scn(systimestamp) FROM dual"):
            try:
                row = self._q1(sql)
                if row and row[0] is not None:
                    return int(row[0])
            except Exception:  # noqa: BLE001
                continue
        return 0

    # --- read-consistency snapshot (chunked load + resume) ---------------
    def capture_snapshot(self) -> Optional[str]:
        """Capture a read-consistency token (the current SCN) at plan time.

        The loader persists it and feeds it to :meth:`use_snapshot`, so every
        count / PK-bound / row read sees ONE consistent snapshot even though the
        load is chunked and resumable — a source mutation mid-run can't leave a
        completed chunk holding stale rows. Returns ``None`` if SCN is
        unavailable (the caller should then quiesce the source)."""
        scn = self.current_scn()
        return str(scn) if scn else None

    def use_snapshot(self, token: Optional[str]) -> None:
        """Pin every subsequent read to the snapshot from :meth:`capture_snapshot`."""
        try:
            self._snapshot_scn = int(token) if token else None
        except (TypeError, ValueError):
            self._snapshot_scn = None

    def _as_of(self) -> str:
        """`AS OF SCN n` flashback clause for the pinned snapshot (or '')."""
        return " AS OF SCN {}".format(self._snapshot_scn) if self._snapshot_scn else ""

    def list_schemas(self) -> List[str]:
        try:
            return [r[0] for r in self._qall(
                "SELECT username FROM all_users WHERE oracle_maintained='N' ORDER BY username"
            )]
        except Exception:  # noqa: BLE001
            return [self.default_schema()]

    # --- introspection ---------------------------------------------------
    def introspect_schema(self, schema: Optional[str] = None) -> Schema:
        owner = (schema or self.default_schema()).upper()
        # A materialized view's container relation is listed in all_tables under
        # the mview name; exclude it so it isn't migrated as a plain table AND
        # surfaced as an mview (it's handled as an mview — review-only).
        mviews = self._materialized_views(owner)
        mview_names = {m.name for m in mviews}
        try:
            tables = [self._table(owner, name) for (name,) in self._qall(
                "SELECT table_name FROM all_tables WHERE owner=:o ORDER BY table_name", o=owner
            ) if name not in mview_names]
            sequences = self._sequences(owner)
            views = self._views(owner)
        except Exception as e:  # noqa: BLE001
            raise IntrospectionError("Oracle introspection failed for {}: {}".format(owner, e)) from e
        # Procedural / advanced objects are surfaced for assessment + review only —
        # v1.0.0 does NOT auto-translate them (PL/SQL -> PL/pgSQL is the v2.0.0
        # roadmap). Each probe is best-effort (see _try_qall): a missing privilege
        # on a dictionary view must never fail an otherwise-valid table/data migration.
        return Schema(
            name=owner, tables=tables, sequences=sequences, views=views,
            mviews=self._materialized_views(owner),
            routines=self._routines(owner), triggers=self._triggers(owner),
        )

    def _table(self, owner: str, name: str) -> Table:
        cols: List[Column] = []
        for (col, dtype, dlen, dprec, dscale, clen, nullable, ddef) in self._qall(
            "SELECT column_name, data_type, data_length, data_precision, data_scale, "
            "char_length, nullable, data_default "
            "FROM all_tab_columns WHERE owner=:o AND table_name=:t ORDER BY column_id",
            o=owner, t=name,
        ):
            src = _reconstruct_source_type(dtype, dlen, dprec, dscale, clen)
            default = None
            if ddef is not None:
                default = str(ddef).strip()
                if default.upper() == "NULL" or default == "":
                    default = None
            cols.append(Column(
                name=col, data_type=map_oracle_type(src), nullable=(nullable == "Y"),
                default=default, source_type=src,
            ))

        pk_cols = [r[0] for r in self._qall(
            "SELECT cc.column_name FROM all_constraints c "
            "JOIN all_cons_columns cc ON c.owner=cc.owner AND c.constraint_name=cc.constraint_name "
            "WHERE c.owner=:o AND c.table_name=:t AND c.constraint_type='P' ORDER BY cc.position",
            o=owner, t=name,
        )]
        primary_key = PrimaryKey(columns=pk_cols) if pk_cols else None

        fks: List[ForeignKey] = []
        for (cname, rtable, rowner, rcons) in self._qall(
            "SELECT c.constraint_name, rc.table_name, rc.owner, rc.constraint_name "
            "FROM all_constraints c JOIN all_constraints rc "
            "  ON c.r_owner=rc.owner AND c.r_constraint_name=rc.constraint_name "
            "WHERE c.owner=:o AND c.table_name=:t AND c.constraint_type='R'",
            o=owner, t=name,
        ):
            local = [r[0] for r in self._qall(
                "SELECT column_name FROM all_cons_columns "
                "WHERE owner=:o AND constraint_name=:c ORDER BY position", o=owner, c=cname,
            )]
            # Referenced columns come from the *referenced constraint*
            # (r_constraint_name) — which may be a UNIQUE key, not necessarily the
            # parent's PRIMARY KEY. Reading the parent PK would emit a wrong FK.
            ref = [r[0] for r in self._qall(
                "SELECT column_name FROM all_cons_columns "
                "WHERE owner=:ro AND constraint_name=:rc ORDER BY position",
                ro=rowner, rc=rcons,
            )]
            fks.append(ForeignKey(
                name=cname, columns=local, references_table=rtable, references_columns=ref,
            ))

        notnull_cols = {c.name.upper() for c in cols if not c.nullable}
        constraints: List[Constraint] = []
        for (cname, cond) in self._qall(
            "SELECT constraint_name, search_condition_vc FROM all_constraints "
            "WHERE owner=:o AND table_name=:t AND constraint_type='C'", o=owner, t=name,
        ):
            text = (cond or "").strip()
            if not text:
                continue
            # Drop ONLY a single-column `<col> IS NOT NULL` check whose column is in
            # fact NOT NULL — that's Oracle's system-generated NOT NULL check, already
            # modeled on the column. A multi-term check (e.g. `email IS NULL OR phone
            # IS NOT NULL`) or a check on a NULLABLE column is a real user CHECK and is
            # preserved. Uses column-nullability metadata, not the regex alone.
            mnn = _GENERATED_NOTNULL.match(text)
            if mnn and mnn.group(1).strip('"').upper() in notnull_cols:
                continue
            constraints.append(Constraint(
                constraint_type=ConstraintKind.CHECK, name=cname, expression=text,
            ))

        indexes: List[Index] = []
        for (iname, uniq) in self._qall(
            "SELECT index_name, uniqueness FROM all_indexes "
            "WHERE table_owner=:o AND table_name=:t", o=owner, t=name,
        ):
            icols = [IndexColumn(name=r[0]) for r in self._qall(
                "SELECT column_name FROM all_ind_columns "
                "WHERE index_owner=:o AND index_name=:i ORDER BY column_position", o=owner, i=iname,
            )]
            if icols:
                indexes.append(Index(name=iname, columns=icols, unique=(uniq == "UNIQUE")))

        return Table(
            name=name, schema=owner, columns=cols, primary_key=primary_key,
            foreign_keys=fks, indexes=indexes, constraints=constraints,
            options=TableOptions(partition=self._partition_info(owner, name)),
        )

    def _sequences(self, owner: str) -> List[SequenceObj]:
        out: List[SequenceObj] = []
        for (sname, minv, maxv, inc, cyc, cache, lastn) in self._qall(
            "SELECT sequence_name, min_value, max_value, increment_by, cycle_flag, "
            "cache_size, last_number FROM all_sequences WHERE sequence_owner=:o", o=owner,
        ):
            # START WITH the current LAST_NUMBER (the next value Oracle would
            # allocate), not min_value — otherwise nextval on the migrated sequence
            # restarts at the bottom and collides with already-migrated IDs.
            start = int(lastn) if lastn is not None else int(minv or 1)
            out.append(SequenceObj(
                name=sname, schema=owner, start=start, increment=int(inc or 1),
                min_value=int(minv) if minv is not None else None,
                max_value=int(maxv) if maxv is not None else None,
                cache=int(cache or 0), cycle=(cyc == "Y"),
            ))
        return out

    def _views(self, owner: str) -> List[View]:
        out: List[View] = []
        for (vname, text) in self._qall(
            "SELECT view_name, text FROM all_views WHERE owner=:o", o=owner,
        ):
            out.append(View(name=vname, schema=owner, definition=str(text or "").strip()))
        return out

    # --- procedural / advanced objects (assess + review only; never auto-applied) ---
    def _try_qall(self, sql: str, **binds: object) -> List[Tuple]:
        """Like :meth:`_qall` but swallows errors (returns ``[]``).

        Procedural-object probes must be best-effort: a role without privileges on
        ``all_source`` / ``all_triggers`` / ``all_mviews`` / ``all_part_tables``
        (or a LONG-column quirk) degrades to "nothing found", never failing the
        table + data migration, which is the part that must always succeed.
        """
        try:
            return self._qall(sql, **binds)
        except Exception:  # noqa: BLE001
            return []

    def _partition_info(self, owner: str, name: str) -> Optional[PartitionInfo]:
        """Detect Oracle partitioning so ``assess`` can flag it.

        v1.0.0 migrates a partitioned table's DATA into a single target table (the
        parent SELECT returns every partition's rows); the partitioning SCHEME is
        not recreated. Capturing the type/key/partition-names lets the assessment
        emit a gap so the partitioning can be recreated on the target if wanted.
        """
        rows = self._try_qall(
            "SELECT partitioning_type FROM all_part_tables WHERE owner=:o AND table_name=:t",
            o=owner, t=name)
        if not rows:
            return None
        ptype = {"RANGE": PartitionType.RANGE, "LIST": PartitionType.LIST,
                 "HASH": PartitionType.HASH}.get(
                     str(rows[0][0] or "").upper().split("-")[0].strip(), PartitionType.RANGE)
        cols = [r[0] for r in self._try_qall(
            "SELECT column_name FROM all_part_key_columns "
            "WHERE owner=:o AND name=:t ORDER BY column_position", o=owner, t=name)]
        parts = [Partition(name=str(r[0]), value="") for r in self._try_qall(
            "SELECT partition_name FROM all_tab_partitions "
            "WHERE table_owner=:o AND table_name=:t ORDER BY partition_position",
            o=owner, t=name)]
        return PartitionInfo(partition_type=ptype, columns=cols, partitions=parts)

    def _routines(self, owner: str) -> List[Routine]:
        """Capture PROCEDURE/FUNCTION/PACKAGE source verbatim, for review.

        Bodies are NOT translated (that is the v2.0.0 roadmap) — they are counted
        by ``assess``, flagged as gaps, and written to a ``.review.sql`` companion
        by ``export`` for manual PL/pgSQL porting. A FUNCTION maps to
        ``RoutineKind.FUNCTION`` and everything else (procedure / package spec /
        package body) to PROCEDURE; the verbatim source (its first line names the
        real object type) is the precise artifact.
        """
        by_obj: dict = {}
        order: list = []
        for (oname, otype, text) in self._try_qall(
            "SELECT name, type, text FROM all_source "
            "WHERE owner=:o AND type IN ('PROCEDURE','FUNCTION','PACKAGE','PACKAGE BODY') "
            "ORDER BY name, type, line", o=owner):
            key = (str(oname), str(otype))
            if key not in by_obj:
                by_obj[key] = []
                order.append(key)
            by_obj[key].append(str(text or ""))
        out: List[Routine] = []
        for (oname, otype) in order:
            kind = RoutineKind.FUNCTION if otype == "FUNCTION" else RoutineKind.PROCEDURE
            body = "".join(by_obj[(oname, otype)]).strip()
            out.append(Routine(name=oname, kind=kind, body=body, language="plsql", schema=owner))
        return out

    def _triggers(self, owner: str) -> List[Trigger]:
        """Capture trigger metadata + body verbatim, for review (not translated)."""
        out: List[Trigger] = []
        for (tname, table, ttype, tevent, tbody) in self._try_qall(
            "SELECT trigger_name, table_name, trigger_type, triggering_event, trigger_body "
            "FROM all_triggers WHERE owner=:o", o=owner):
            tt = str(ttype or "").upper()
            timing = "INSTEAD OF" if "INSTEAD OF" in tt else ("AFTER" if tt.startswith("AFTER") else "BEFORE")
            events = [e.strip().upper() for e in
                      str(tevent or "").replace(" OR ", ",").split(",") if e.strip()]
            out.append(Trigger(name=str(tname), table=str(table or ""), timing=timing,
                               events=events, body=str(tbody or "").strip(), schema=owner))
        return out

    def _materialized_views(self, owner: str) -> List[View]:
        """Capture materialized-view names + defining queries, for review."""
        out: List[View] = []
        for (mname, query) in self._try_qall(
            "SELECT mview_name, query FROM all_mviews WHERE owner=:o", o=owner):
            out.append(View(name=str(mname), definition=str(query or "").strip(),
                            materialized=True, schema=owner))
        return out

    # --- extraction ------------------------------------------------------
    def exact_row_count(self, table: Table) -> int:
        owner = (table.schema or self.default_schema()).upper()
        tbl = quote_oracle(owner, table.name)
        row = self._q1_consistent(lambda a: "SELECT COUNT(*) FROM {}{}".format(tbl, a))
        return int(row[0]) if row else 0

    def numeric_pk_bounds(self, table: Table, pk_col: str):
        owner = (table.schema or self.default_schema()).upper()
        tbl = quote_oracle(owner, table.name)
        row = self._q1_consistent(
            lambda a: "SELECT MIN({c}), MAX({c}) FROM {t}{a}".format(c=_oid(pk_col), t=tbl, a=a))
        if not row or row[0] is None:
            return None
        try:
            # floor() rather than int() (which truncates toward zero): a negative
            # fractional MIN like -2.75 must floor to -3 so the chunk predicate
            # `pk >= -3` still covers it. int(-2.75) == -2 would skip that row.
            # Integer PKs are unchanged; half-open integer ranges cover any
            # fractional values that fall between the bounds.
            return math.floor(row[0]), math.floor(row[1])
        except (TypeError, ValueError):
            return None  # non-numeric PK -> caller falls back to a single chunk

    def stream_rows(
        self,
        table: Table,
        columns: Sequence[str],
        where: Optional[str] = None,
        arraysize: int = 1000,
    ) -> Iterator[Tuple]:
        owner = (table.schema or self.default_schema()).upper()
        col_list = ", ".join(_oid(c) for c in columns)
        base = "SELECT {} FROM {}".format(col_list, quote_oracle(owner, table.name))
        tail = " WHERE {}".format(where) if where else ""
        cur = self.conn.cursor()
        cur.arraysize = arraysize
        cur.prefetchrows = arraysize + 1
        try:
            try:
                cur.execute(base + self._as_of() + tail)
            except Exception as e:  # noqa: BLE001
                # Flashback read can't see a table DDL'd after the snapshot
                # (ORA-01466); fall back to a current read for this table.
                if not (self._snapshot_scn and _is_flashback_error(e)):
                    raise
                _LOG.warning("AS OF SCN %s read of %s failed (%s); falling back to a "
                             "current read — read-consistency not guaranteed for this table",
                             self._snapshot_scn, quote_oracle(owner, table.name),
                             str(e).splitlines()[0][:90])
                cur.execute(base + tail)
            while True:
                batch = cur.fetchmany(arraysize)
                if not batch:
                    break
                for row in batch:
                    yield tuple(row)
        finally:
            cur.close()
