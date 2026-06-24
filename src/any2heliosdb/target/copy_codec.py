"""PostgreSQL COPY text-format codec.

Mirrors HeliosDB's own escaping (``Lite/src/protocol/postgres/copy.rs``) so the
data files this tool emits round-trip exactly through HeliosDB's COPY parser:

* ``\\`` → ``\\\\``, newline → ``\\n``, tab → ``\\t``, CR → ``\\r``
* a NULL field is the ``null_string`` (default ``\\N``); an *empty string* is an
  empty field — distinct from NULL. Preserving this distinction is the load-side
  half of the Oracle "empty string == NULL" gotcha.

The live psycopg COPY path adapts Python values itself; this codec is for the
file-export path (``export -t COPY``) and the wizard's NULL/'' fidelity check.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

DEFAULT_NULL = "\\N"
DEFAULT_DELIMITER = "\t"


def escape_copy_text(s: str) -> str:
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        else:
            out.append(ch)
    return "".join(out)


def unescape_copy_text(s: str) -> str:
    out: List[str] = []
    it = iter(s)
    for ch in it:
        if ch == "\\":
            nxt = next(it, None)
            if nxt == "n":
                out.append("\n")
            elif nxt == "t":
                out.append("\t")
            elif nxt == "r":
                out.append("\r")
            elif nxt == "\\":
                out.append("\\")
            elif nxt is None:
                out.append("\\")
            else:
                out.append("\\")
                out.append(nxt)
        else:
            out.append(ch)
    return "".join(out)


def encode_field(value: Optional[object], null_string: str = DEFAULT_NULL) -> str:
    if value is None:
        return null_string
    return escape_copy_text(value if isinstance(value, str) else str(value))


def encode_row(
    values: Sequence[Optional[object]],
    delimiter: str = DEFAULT_DELIMITER,
    null_string: str = DEFAULT_NULL,
) -> str:
    return delimiter.join(encode_field(v, null_string) for v in values) + "\n"


def decode_row(
    line: str,
    delimiter: str = DEFAULT_DELIMITER,
    null_string: str = DEFAULT_NULL,
) -> List[Optional[str]]:
    line = line.rstrip("\n")
    fields: List[Optional[str]] = []
    for raw in line.split(delimiter):
        if raw == null_string:
            fields.append(None)
        else:
            fields.append(unescape_copy_text(raw))
    return fields
