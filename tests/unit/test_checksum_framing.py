"""Regression test for CODEX finding #58: row-hash delimiter collision.

``row_checksum`` used to join rendered field values with a single ``\\x01``
delimiter byte and no length framing, so two different rows whose rendered
fields differ only in where a ``\\x01`` falls produced the *same* joined byte
stream and hashed identically — letting TEST_DATA silently MISS a real data
mismatch (false negative). The fix length-frames every field; these tests pin
that two such colliding tuples now hash DIFFERENTLY while equal rows still match.
"""
from any2heliosdb.validate.data import _FIELD_SEP, row_checksum


def test_delimiter_boundary_collision_now_differs():
    # The canonical collision from the finding: with a bare _FIELD_SEP join both
    # rows serialize to the identical stream b"a\\x01b\\x01c".
    sep = _FIELD_SEP
    a = ("a", "b" + sep + "c")   # -> a | b<sep>c
    b = ("a" + sep + "b", "c")   # -> a<sep>b | c
    # Sanity: this really is the historical collision (same naive join).
    assert sep.join(a) == sep.join(b)
    # ...but the framed row hashes must now differ.
    assert row_checksum(a) != row_checksum(b)


def test_delimiter_inside_a_field_does_not_shift_boundary():
    # A field that *contains* the separator must not be confusable with the
    # multi-field row that splits at that separator.
    sep = _FIELD_SEP
    assert row_checksum([sep.join(["x", "y", "z"])]) != row_checksum(["x", "y", "z"])
    assert row_checksum(["p" + sep, "q"]) != row_checksum(["p", sep + "q"])


def test_empty_field_boundaries_are_unambiguous():
    # Arity / empty-field shifts that a bare join would also collapse.
    assert row_checksum(["", "a"]) != row_checksum(["", "", "a"])
    assert row_checksum(["a", ""]) != row_checksum(["a"])


def test_identical_field_tuples_still_hash_equal():
    # Determinism: equal input (the same code runs on source and target) -> equal hash.
    assert row_checksum(("a", "b" + _FIELD_SEP + "c")) == \
        row_checksum(("a", "b" + _FIELD_SEP + "c"))
    assert row_checksum([1, "alice", None]) == row_checksum([1, "alice", None])
    assert row_checksum(["", "", "a"]) == row_checksum(["", "", "a"])
