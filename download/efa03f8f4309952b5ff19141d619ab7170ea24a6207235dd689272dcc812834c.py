"""Source-backed guards for a definition-reference proposal.

This is a bounded evidence check, not a general natural-language entailment
engine. A checked join proves a record pair; it does not prove that the target
owns the definition. Unrecognized or contradictory declarations stay unresolved.
The caller still validates quotes, technical counts, transforms and snapshots.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping


_REFERENCE = re.compile(
    r"引用|对应|指向|参照|参考|外键|\b(?:references?|refers?\s+to|points?\s+to|foreign\s+key)\b", re.I)
_NEGATED_REFERENCE = re.compile(
    r"(?:不|未|无需|无须|禁止)(?:直接)?(?:引用|对应|指向|参照|参考)|"
    r"\b(?:not|never|no)\b.{0,16}\b(?:refer\w*|point\w*|foreign\s+key)\b", re.I)
_DEFINITION = re.compile(r"定义|\bdefinitions?\b", re.I)
_DEFINITION_TABLE = re.compile(
    r"定义(?:表|库|集合|目录)?[。.]?$|\bdefinitions?(?:\s+(?:table|catalog|registry|dictionary))?[.]?$", re.I)
_NEGATED_DEFINITION = re.compile(
    r"(?:不|非|并非)(?:是|为)?(?:本身的)?定义|\bnot\s+(?:a\s+)?definition\b", re.I)
_DEFINING_TEXT = re.compile(
    r"定义为|计算(?:口径)?为|定义[：:]|\b(?:defined|calculated)\s+as\b", re.I)
_DEFINING_FIELD = re.compile(r"定义|计算公式|计算口径|\b(?:definition|formula)\b", re.I)
_KEY = re.compile(r"编码|标识|主键|\b(?:code|identifier|key|id)\b", re.I)
_USE_CONTEXT = re.compile(r"用于|按.+(?:组合|统计|取数|计算)|\b(?:uses?|lookup|retrieve\w*)\b", re.I)
_NEGATED_USE = re.compile(
    r"(?:不|未|非|无需|无须|禁止)(?:再|直接)?(?:用于|使用|采用|按)|"
    r"\b(?:not|never|no)\b.{0,16}\b(?:us\w*|lookup|retriev\w*)\b", re.I)
_EXPLICIT_OUTBOUND = re.compile(
    r"引用|指向|参照|参考|外键|其他|其它|另一个|另一|外部|第三方|非本|"
    r"\b(?:references?|refers?\s+to|points?\s+to|foreign\s+key|other|another|external)\b", re.I)


def _raw(value):
    return "" if value is None else str(value)


def _text(value):
    return unicodedata.normalize("NFKC", _raw(value)).strip()


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _entries(record):
    for role, entries in record.get("fields", {}).items():
        for entry in entries:
            if isinstance(entry, dict) and not entry.get("truncated") and _text(entry.get("value")):
                yield role, entry


def _schema(data, record, column=None):
    """Prefer the actual dataset declaration, including its explicit absence."""
    table = getattr(data, "tables", {}).get(record["table"])
    if table is not None:
        if column is None:
            return _raw(table.get("table_comment"))
        return next((_raw(item.get("column_comment")) for item in table.get("columns", [])
                     if item.get("column_name") == column), "")
    return next((_raw(entry.get("column_comment")) for _, entry in _entries(record)
                 if entry.get("column") == column), "") if column else ""


def _referents(text):
    """Read explicit destinations, without guessing one from a table name."""
    result = []
    for match in re.finditer(r"(?:引用|对应|指向|参照|参考|references?\s+|refers?\s+to\s+|points?\s+to\s+)([^,，;；。\n]+)", text, re.I):
        value = match.group(1).strip().casefold()
        value = re.split(r"表(?:的|中)|\s+(?:table|column)\b", value)[0].strip()
        value = re.sub(r"^(?:the\s+|本表的?)", "", value)
        if value and value not in {"编码", "标识", "code", "key", "id"}:
            result.append(value)
    return result


def _points_to_self(declaration, target_record, table_declaration):
    # A real table identifier is usable only if the source declaration names it.
    identities = [target_record["table"].casefold(), target_record["table"].split(".")[-1].casefold()]
    context = table_declaration.casefold()
    return any(any(name and name in ref for name in identities)
               or (len(ref) >= 4 and ref in context) for ref in _referents(declaration))


def _weak_key_correspondence(declaration):
    """A catalog's generic correspondence is not a proven foreign owner.

    This exception never covers named tables, qualified identifiers, explicit
    reference verbs or external owners. The caller must separately prove the
    target's own declared purpose and complete definition body.
    """
    referents = _referents(declaration)
    return ("对应" in declaration and not _EXPLICIT_OUTBOUND.search(declaration)
            and bool(referents)
            and not re.search(r"表|\btable\b|[._]", declaration.split("对应", 1)[1], re.I))


def _owns_definition_table(declaration):
    # A title may append other fields after its declared definition purpose.
    purpose = re.split(r"[,，、;；:：]", declaration, maxsplit=1)[0].strip()
    return bool(_DEFINITION_TABLE.search(purpose)
                and not _REFERENCE.search(declaration)
                and not _NEGATED_DEFINITION.search(declaration))


def assess_definition_reference(data, bundle, decision, source_record, target_record):
    """Return an auditable supported/unresolved/not_applicable evidence report.

    Only ``points_to/definition_reference`` is checked. No predicate, source
    input, dataset evidence or plan is mutated. Schema statements may support a
    name-only quote, but names and a common value alone are never sufficient.
    """
    audit = {"contract": "definition_reference_evidence_v1", "status": "not_applicable",
             "reason_codes": [], "rule_id": (bundle.get("rule") or {}).get("rule_id"),
             "source_record_id": source_record.get("record_id"),
             "target_record_id": target_record.get("record_id"), "evidence": []}
    if (_get(decision, "parent_relation") != "points_to"
            or _get(decision, "predicate_name") != "definition_reference"):
        return audit

    def evidence(record, column, value, *, origin="declared_metadata"):
        item = {"origin": origin, "table": record["table"], "column": column,
                "raw_fragment": value}
        if origin == "declared_metadata":
            item["evidence_id"] = f"schema:{record['table']}" + (f":{column}" if column else "")
        else:
            item.update(record_id=record["record_id"], row=record.get("row_number"))
        if item not in audit["evidence"]:
            audit["evidence"].append(item)

    def unresolved(reason):
        audit.update(status="unresolved", reason_codes=[reason])
        return audit

    rule = bundle.get("rule") or {}
    if (rule.get("status") not in {"checked_technical", "observed_subset"}
            or rule.get("verification", {}).get("scan_scope") != "full_input"
            or bundle.get("snapshot_id") != getattr(data, "snapshot_id", None)
            or rule.get("snapshot_id") != getattr(data, "snapshot_id", None)):
        return unresolved("definition_reference_requires_current_full_input_check")
    if not any(pair.get("source_record_id") == source_record.get("record_id")
               and pair.get("target_record_id") == target_record.get("record_id")
               for pair in bundle.get("examples", {}).get("positive", [])):
        return unresolved("definition_reference_requires_checked_witness_pair")
    source_field = (rule.get("source") or {}).get("field")
    target_field = (rule.get("target") or {}).get("field")
    source_decl = _text(_schema(data, source_record, source_field))
    target_decl = _text(_schema(data, target_record, target_field))
    target_table_decl = _text(_schema(data, target_record))
    for record, column, value in ((source_record, source_field, source_decl),
                                  (target_record, target_field, target_decl),
                                  (target_record, None, target_table_decl)):
        if value:
            evidence(record, column, _schema(data, record, column))

    if _NEGATED_REFERENCE.search(source_decl):
        return unresolved("source_declaration_negates_reference")
    target_outbound = bool(_REFERENCE.search(target_decl))
    definition_entries = []
    for role, entry in _entries(target_record):
        if role not in {"description", "formula"}:
            continue
        value = _text(entry["value"])
        declaration = _text(_schema(data, target_record, entry["column"]))
        if _NEGATED_DEFINITION.search(value) or _REFERENCE.search(value):
            continue
        if ((_DEFINING_FIELD.search(declaration) and not _REFERENCE.search(declaration)
             and not _NEGATED_DEFINITION.search(declaration)) or _DEFINING_TEXT.search(value)):
            definition_entries.append(entry)
    owns_table = _owns_definition_table(target_table_decl)
    if target_outbound and not _points_to_self(target_decl, target_record, target_table_decl):
        if not (_weak_key_correspondence(target_decl) and owns_table and definition_entries):
            same_destinations = set(_referents(source_decl)) & set(_referents(target_decl))
            return unresolved("both_endpoints_reference_third_party" if same_destinations
                              else "target_join_field_references_another_definition")
        audit["target_key_interpretation"] = "own_definition_with_weak_correspondence"
        for entry in definition_entries:
            declaration = _schema(data, target_record, entry["column"])
            if declaration:
                evidence(target_record, entry["column"], declaration)
            evidence(target_record, entry["column"], _raw(entry["value"]), origin="observed_record")

    # A source key's declaration can express reference intent without an FK.
    source_support = bool(_REFERENCE.search(source_decl))
    target_names = [_text(entry["value"]) for role, entry in _entries(target_record)
                    if role in {"name", "alias"}]
    if not source_support:
        for role, entry in _entries(source_record):
            value = _text(entry["value"])
            if (role in {"description", "context"} and _REFERENCE.search(value)
                    and not _NEGATED_REFERENCE.search(value)
                    and any(name and name in value for name in target_names)):
                source_support = True
                evidence(source_record, entry["column"], _raw(entry["value"]), origin="observed_record")
    if not source_support and _KEY.search(source_decl):
        # The selected witness record's full usage is evidence even when the
        # model quotes its name. Never borrow purpose from a different record;
        # quote validity remains the caller's separate compilation check.
        for role, entry in _entries(source_record):
            value = _text(entry["value"])
            if (role in {"description", "context"} and _USE_CONTEXT.search(value)
                    and not _NEGATED_REFERENCE.search(value) and not _NEGATED_USE.search(value)):
                source_support = True
                evidence(source_record, entry["column"], _raw(entry["value"]), origin="observed_record")
    if not source_support:
        return unresolved("source_reference_intent_not_established")

    # Table/field declarations must describe the target's own definition, not
    # a container that merely references definitions elsewhere.
    target_support = bool(
        owns_table
        or (_DEFINITION.search(target_decl) and _KEY.search(target_decl)
            and not target_outbound and not _NEGATED_DEFINITION.search(target_decl)))
    if not target_support:
        for entry in definition_entries:
            target_support = True
            declaration = _schema(data, target_record, entry["column"])
            if declaration:
                evidence(target_record, entry["column"], declaration)
            evidence(target_record, entry["column"], _raw(entry["value"]), origin="observed_record")
    if not target_support:
        return unresolved("target_definition_ownership_not_established")
    audit.update(status="supported", reason_codes=["source_reference_and_target_definition_supported"])
    return audit


class RelationEvidenceError(ValueError):
    """An unresolved proposal carries the complete local evidence audit."""

    def __init__(self, audit):
        self.audit = audit
        super().__init__("Definition reference unresolved: " + "; ".join(audit["reason_codes"]))


def validate_definition_reference(data, bundle, decision, source_record, target_record):
    """Raise before plan construction; return the report for persistence otherwise."""
    audit = assess_definition_reference(data, bundle, decision, source_record, target_record)
    if audit["status"] == "unresolved":
        raise RelationEvidenceError(audit)
    return audit
