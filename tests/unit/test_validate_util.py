"""The shared ``effective_preserve_case`` helper: one definition drives both the
CLI ``test-count`` / ``test-data`` / ``test-index`` commands and the MCP
``test_count`` / ``test_data`` tools, so the two surfaces can never disagree on
identifier case (the interface-honesty defect this replaces).
"""
import types

from any2heliosdb.validate.util import effective_preserve_case


def _cfg(preserve_case):
    return types.SimpleNamespace(options=types.SimpleNamespace(preserve_case=preserve_case))


def _tgt(dialect):
    return types.SimpleNamespace(dialect=dialect)


def test_native_oracle_target_forces_preserve_case():
    # The native (Oracle-wire) target keeps source-case names even with the option
    # off — a lowercase-folding validator would query a missing relation.
    assert effective_preserve_case(_cfg(False), _tgt("oracle")) is True


def test_postgres_target_respects_option_false():
    assert effective_preserve_case(_cfg(False), _tgt("postgres")) is False


def test_option_true_wins_regardless_of_dialect():
    assert effective_preserve_case(_cfg(True), _tgt("postgres")) is True


def test_missing_dialect_attribute_defaults_to_option():
    # A target driver without a `dialect` attr (e.g. a bare stub) is not native.
    assert effective_preserve_case(_cfg(False), object()) is False
    assert effective_preserve_case(_cfg(True), object()) is True
