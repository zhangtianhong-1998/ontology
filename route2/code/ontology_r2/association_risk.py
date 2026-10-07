"""A missing value-risk assessment is unknown, never proof of safe identity."""

import re

# Shared by recall, SQL verification and alias ordering. This classifies
# coincidence risk only; it never normalizes the identity used by a join.
NUMERIC_MATCH_PATTERN = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?%?"


def is_numeric_match_value(value):
    return isinstance(value, str) and re.fullmatch(NUMERIC_MATCH_PATTERN, value.strip()) is not None


def numeric_overlap_state(record):
    value = record.get("numeric_overlap_only")
    return value if type(value) is bool else None


def numeric_overlap_requires_evidence(record):
    """Numeric-only and unassessed matches both need independent path evidence."""
    return numeric_overlap_state(record) is not False
