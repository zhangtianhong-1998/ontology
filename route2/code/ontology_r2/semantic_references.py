"""Replayable lexical references, kept separate from business type identity.

A Term denotes an observed member, which belongs to a Dimension. JSON keys
are discovered from complete source documents; neither shared column names
nor coincident values suffice to certify this two-hop semantic path.
"""
from __future__ import annotations

from collections import defaultdict
import re
import unicodedata

from .column_roles import declared_context_role, is_sensitive_column
from .configuration_relations import _type_alignments
from .discovery import discover_json_reference_candidates, json_scalar_paths, validate_candidate
from .models import BuildPlan, DerivedType, RelationPlan, WitnessedPair
from .relation_contract import canonical_relation_definition, canonical_relation_id
from .storage import digest, qi


_MEMBER_CODE = re.compile(r"成员(?:编码|标识)|member\s+(?:code|identifier|key)", re.I)
_LEXICAL = re.compile(r"名称|别名|同义词|\b(?:name|label|alias|aliases|synonym|synonyms|term|word)\b", re.I)
_ALIAS = re.compile(r"别名|同义词|\b(?:alias|aliases|synonym|synonyms)\b", re.I)
_CODE = re.compile(r"编码|标识|\b(?:code|identifier|key)\b", re.I)
_SCOPE = re.compile(r"所属|内解释|作用域|within|scoped|namespace", re.I)


def _normal(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def _columns(data, table):
    info = data.tables[table]
    return [column for column in info["columns"]
            if column["column_name"] not in info.get("semantic_excluded_columns", ())
            and not is_sensitive_column(column)]


def _lexical_columns(data, table):
    return [column for column in _columns(data, table)
            if _LEXICAL.search(str(column.get("column_comment") or "") + " "
                               + column["column_name"].replace("_", " "))]


def _member_specs(data):
    """Recognize declared member keys and their explicitly named namespaces."""
    output = []
    for table in sorted(data.tables):
        columns = _columns(data, table)
        for column in columns:
            declaration = str(column.get("column_comment") or "")
            if not _MEMBER_CODE.search(declaration) or not _SCOPE.search(declaration):
                continue
            scopes = [item["column_name"] for item in columns
                      if item != column and re.search(
                          r"(?<![\w])" + re.escape(item["column_name"]) + r"(?![\w])", declaration)]
            if len(scopes) == 1 and _lexical_columns(data, table):
                output.append({"table": table, "field": column["column_name"],
                               "scope_column": scopes[0], "declaration": declaration})
    return output


def _lookup(data, table, bindings):
    """Fresh bounded lookup, including on replay after source mutation."""
    if not bindings or any(value in (None, "") for _, value in bindings):
        return []
    if any(column not in data.tables[table]["column_names"] for column, _ in bindings):
        raise ValueError("Unknown semantic reference lookup column")
    cursor = data.db.execute(f"SELECT * FROM {qi(data.tables[table]['sql_name'])} WHERE "
        + " AND ".join(f"{qi(column)}=?" for column, _ in bindings) + " LIMIT 2",
        [value for _, value in bindings])
    return [dict(zip((item[0] for item in cursor.description), row)) for row in cursor.fetchall()]


def _row(data, table, number):
    cursor = data.db.execute(f"SELECT * FROM {qi(data.tables[table]['sql_name'])} WHERE __r2_row=?", [number])
    values = cursor.fetchone()
    if values is None:
        raise ValueError("Semantic reference source row disappeared")
    return dict(zip((item[0] for item in cursor.description), values))


def _proof(data, table, row, column):
    """Store the whole original cell; path extraction is independently replayed."""
    record_id = data.record_id(table, row)
    evidence_id = "record:" + digest([data.snapshot_id, record_id, column])[:24]
    evidence = {"id": evidence_id, "origin": "observed_record", "raw_fragment": row[column],
                "raw_fragment_truncated": False, "source_ref": {
                    "table": table, "column": column, "row": row["__r2_row"],
                    "record_id": record_id, "snapshot_id": data.snapshot_id}}
    old = data.evidence.get(evidence_id)
    if old and (old.get("raw_fragment") != evidence["raw_fragment"]
                or old.get("source_ref") != evidence["source_ref"]):
        raise ValueError("Conflicting source evidence for semantic reference")
    data.evidence.setdefault(evidence_id, evidence)
    return evidence_id


def _identity_conflict(data, left_table, left, right_table, right):
    """An explicit namespace/tenant/version is never discarded to obtain a match."""
    sides = []
    for table, row in ((left_table, left), (right_table, right)):
        sides.append({column["column_name"]: row.get(column["column_name"])
                      for column in _columns(data, table) if declared_context_role(column) == "identity"})
    return any(sides[0].get(key) in (None, "") or sides[1].get(key) in (None, "")
               or sides[0][key] != sides[1][key] for key in set(sides[0]) | set(sides[1]))


def _lexical_match(data, term, member_table, member):
    for column in _lexical_columns(data, member_table):
        name = column["column_name"]
        value = member.get(name)
        if not isinstance(value, str):
            continue
        declaration = str(column.get("column_comment") or "") + " " + name.replace("_", " ")
        pieces = re.split(r"[,;，；、|]", value) if _ALIAS.search(declaration) else [value]
        if _normal(term) in {_normal(part) for part in pieces if part.strip()}:
            return name
    return None


def membership_declaration(value, dimension_label, member_labels):
    """Recognize complete affirmative source declarations, never keyword cooccurrence.

    This deliberately narrow grammar is replayable. Other prose (negation,
    modality, conditions, or extra clauses) needs semantic review rather than
    accumulating a blacklist of a few negative words.
    """
    if not isinstance(value, str) or not dimension_label:
        return False
    sentence = _normal(value).rstrip(".。")
    axis = re.escape(_normal(dimension_label))
    for label in member_labels:
        if not isinstance(label, str) or not label.strip():
            continue
        name = re.escape(_normal(label))
        patterns = (rf"{axis}\s*成员\s*[:：]\s*{name}",
                    rf"{axis}\s+member\s*:\s*{name}",
                    rf"{name}\s*(?:是|属于)\s*{axis}\s*(?:的)?成员",
                    rf"{name}\s+is\s+(?:a\s+)?member\s+of\s+{axis}")
        if any(re.fullmatch(pattern, sentence) for pattern in patterns):
            return True
    return False


def _membership_text(data, member_table, member, dimension_label):
    labels = []
    for column in _lexical_columns(data, member_table):
        value = member.get(column["column_name"])
        declaration = str(column.get("column_comment") or "") + " " + column["column_name"]
        if isinstance(value, str):
            labels.extend(re.split(r"[,;，；、|]", value) if _ALIAS.search(declaration) else [value])
    for column in _columns(data, member_table):
        if membership_declaration(member.get(column["column_name"]), dimension_label, labels):
            return column["column_name"]
    return None


def json_member_scope(leaves, source_path, scope_column):
    """Only a sibling field in the exact reference object can qualify its key.

    A same-named leaf in an audit branch, ancestor, or another array/object
    does not establish a scope mapping. Preserve the full path for replay.
    """
    path = (*tuple(source_path)[:-1], scope_column)
    if path == tuple(source_path) or path not in leaves:
        return None
    return {"source_path": list(path), "target_column": scope_column,
            "value": leaves[path], "structural_contract": "same_json_reference_object"}


def _node(data, table, row, *, kind, label, type_id, evidence_ids):
    record_id = data.record_id(table, row)
    return {"id": "semantic_instance:" + digest([data.snapshot_id, kind, record_id, type_id])[:24],
            "instance_kind": kind, "label": label, "ontology_type_id": type_id, "type": type_id,
            "ontology_level": "instance", "source_ref": {"table": table,
                "record_id": record_id, "row": row["__r2_row"], "snapshot_id": data.snapshot_id},
            "evidence_ids": sorted(set(evidence_ids)), "identity_scope": "source_record",
            "business_observation_claim": False,
            "type_role": "member_of_dimension" if kind == "dimension_member" else "definition_record",
            "semantic_role": {"dimension_member": "observed_dimension_member_not_dimension_definition",
                              "lexical_term": "lexical_expression_not_denoted_entity",
                              "dimension_definition": "dimension_definition_record"}[kind],
            "properties": []}


def build_semantic_references(data, plan, concepts=(), alignments=(), *, memberships=(), options=None):
    """Return a copied plan, source proofs, typed instance paths and bounded coverage.

    This known lexical/member contract needs no LLM. Unrecognized patterns are
    pending, never guessed. Limits bound discovery and source-type rows; a
    certified path still checks its code uniqueness against the whole table.
    """
    options = options or {}
    defaults = {"max_source_rows_per_table": 10000, "max_paths": 64,
                "max_candidates": 128, "max_target_fields": 128}
    if set(options) - set(defaults):
        raise ValueError("Unknown semantic reference option")
    limits = {**defaults, **options}
    if any(type(value) is not int or value < 1 for value in limits.values()):
        raise ValueError("Semantic reference limits must be positive integers")
    core = BuildPlan.model_validate(plan).model_copy(deep=True)
    types = {item.id: item for item in core.object_types if item.category == "business_type"}
    accepted, conflicts = _type_alignments(core, concepts, alignments, memberships)
    wanted = {record_id: match for record_id, match in accepted.items()
              if types[match["type_id"]].parent in {"Term", "Dimension"}}
    sources, omissions = [], 0
    for table in sorted({record_id.rpartition(":")[0] for record_id in wanted}):
        if table not in data.tables:
            continue
        total = data.db.execute(f"SELECT count(*) FROM {qi(data.tables[table]['sql_name'])}").fetchone()[0]
        omissions += max(0, total - limits["max_source_rows_per_table"])
        for row in data.rows(table, limit=limits["max_source_rows_per_table"]):
            match = wanted.get(data.record_id(table, row))
            if match:
                sources.append((table, row, types[match["type_id"]], match))
    terms = [source for source in sources if source[2].parent == "Term"]
    dimensions = [source for source in sources if source[2].parent == "Dimension"]
    specs = _member_specs(data)
    recognized = {spec["table"] for spec in specs}
    skipped_member_tables = [{"table": table, "reason": "no_source_declared_member_key_and_named_namespace"}
                             for table in sorted(data.tables) if table not in recognized]
    recalled = discover_json_reference_candidates(data,
        source_tables={source[0] for source in terms},
        target_fields={(spec["table"], spec["field"]) for spec in specs},
        max_rows_per_table=limits["max_source_rows_per_table"], max_paths=limits["max_paths"],
        max_candidates=limits["max_candidates"], max_target_fields=limits["max_target_fields"])
    candidates = recalled["candidates"]
    checks = [validate_candidate(data, candidate) for candidate in candidates]
    by_source = defaultdict(list)
    for candidate in candidates:
        by_source[candidate["source"]["table"]].append(candidate)
    nodes, assertions, paths, relation_proofs, pending = {}, {}, [], defaultdict(list), []
    located_term_types = {source[2].id for source in terms}
    pending.extend({"type_id": item.id, "reason": "accepted_term_type_has_no_verified_source_alignment"}
                   for item in types.values() if item.parent == "Term" and item.id not in located_term_types)
    for table, row, term_type, alignment in terms:
        matches = []
        failure = "no_source_grounded_json_member_path"
        lexical_fields = [column["column_name"] for column in _lexical_columns(data, table)
                          if _normal(row.get(column["column_name"], "")) == _normal(term_type.label)]
        if not lexical_fields:
            pending.append({"type_id": term_type.id, "reason": "term_label_not_in_source_lexical_field"})
            continue
        for candidate in by_source[table]:
            source, target = candidate["source"], candidate["target"]
            leaves = {tuple(path): value for path, value in json_scalar_paths(row.get(source["field"]))}
            code = leaves.get(tuple(source["source_path"]))
            if code is None:
                continue
            spec = next(item for item in specs if item["table"] == target["table"] and item["field"] == target["field"])
            # No source namespace is invented. A code ambiguous anywhere in
            # the target table needs an explicit namespace in the source JSON.
            bindings = [(target["field"], code)]
            namespace = json_member_scope(leaves, source["source_path"], spec["scope_column"])
            if namespace:
                bindings.append((spec["scope_column"], namespace["value"]))
            targets = _lookup(data, target["table"], bindings)
            if len(targets) != 1:
                failure = "member_code_missing_or_ambiguous_in_namespace"
                continue
            member = targets[0]
            if _identity_conflict(data, table, row, target["table"], member):
                failure = "lexical_member_identity_scope_conflict"
                continue
            lexical = _lexical_match(data, term_type.label, target["table"], member)
            if lexical is None:
                failure = "code_match_without_lexical_evidence"
                continue
            axes = []
            for dt, dr, dimension, dm in dimensions:
                if _identity_conflict(data, target["table"], member, dt, dr):
                    continue
                text_column = _membership_text(data, target["table"], member, dimension.label)
                if text_column is None:
                    continue
                for column in _columns(data, dt):
                    name = column["column_name"]
                    if (_CODE.search(str(column.get("column_comment") or ""))
                            and member.get(spec["scope_column"]) not in (None, "")
                            and dr.get(name) == member[spec["scope_column"]]
                            and len(_lookup(data, dt, ((name, dr[name]),))) == 1):
                        axes.append((dt, dr, dimension, dm, name, text_column))
            if len(axes) != 1:
                failure = "member_dimension_missing_or_ambiguous"
                continue
            matches.append((candidate, spec, member, lexical, axes[0], bindings))
        # Multiple candidate paths to the same witnessed member/axis are
        # redundant evidence; distinct endpoints are a semantic ambiguity.
        grouped = defaultdict(list)
        for match in matches:
            candidate, spec, member, lexical, axis, bindings = match
            grouped[(data.record_id(spec["table"], member), axis[2].id)].append(match)
        if len(grouped) != 1:
            pending.append({"type_id": term_type.id, "source_record_id": data.record_id(table, row),
                            "reason": "multiple_member_paths" if grouped else failure})
            continue
        candidate, spec, member, lexical, axis, bindings = next(iter(grouped.values()))[0]
        dt, dr, dimension, dm, dimension_key, membership_text = axis
        source = candidate["source"]
        cells = [(table, row, lexical_fields[0]), (table, row, source["field"]),
                 (spec["table"], member, spec["field"]), (spec["table"], member, lexical),
                 (spec["table"], member, spec["scope_column"]),
                 (spec["table"], member, membership_text), (dt, dr, dimension_key)]
        evidence = sorted({_proof(data, *cell) for cell in cells} |
                          set(alignment["evidence_ids"]) | set(dm["evidence_ids"]))
        schema_evidence = [f"schema:{owner}:{column}" for owner, column in (
            (table, source["field"]),
            (spec["table"], spec["field"]), (spec["table"], spec["scope_column"]),
            (spec["table"], lexical), (dt, dimension_key))]
        evidence = sorted(set(evidence) | set(schema_evidence))
        term_node = _node(data, table, row, kind="lexical_term", label=term_type.label,
                          type_id=term_type.id, evidence_ids=evidence)
        member_label = next((member[column["column_name"]] for column in _lexical_columns(data, spec["table"])
                             if member.get(column["column_name"]) and not _ALIAS.search(
                                 str(column.get("column_comment") or "") + " " + column["column_name"].replace("_", " "))), member[lexical])
        member_node = _node(data, spec["table"], member, kind="dimension_member",
                            label=member_label, type_id=dimension.id, evidence_ids=evidence)
        dimension_node = _node(data, dt, dr, kind="dimension_definition", label=dimension.label,
                               type_id=dimension.id, evidence_ids=evidence)
        proof = {"snapshot_id": data.snapshot_id, "candidate": candidate,
                 "member_spec": spec, "source_lexical_column": lexical_fields[0],
                 "member_lexical_column": lexical, "membership_text_column": membership_text,
                 "dimension_key_column": dimension_key, "member_lookup_bindings": bindings,
                 "member_namespace_binding": json_member_scope(
                     {tuple(path): value for path, value in json_scalar_paths(row.get(source["field"]))},
                     source["source_path"], spec["scope_column"]),
                 "term_type_id": term_type.id, "dimension_type_id": dimension.id,
                 "term_node": term_node["id"], "member_node": member_node["id"],
                 "dimension_node": dimension_node["id"], "evidence_ids": evidence}
        for node in (term_node, member_node, dimension_node):
            if node["id"] in nodes:
                nodes[node["id"]]["evidence_ids"] = sorted(set(nodes[node["id"]]["evidence_ids"]) | set(evidence))
            else:
                nodes[node["id"]] = node
        edge_ids = []
        for left, right, parent, predicate in ((term_node, member_node, "points_to", "denotes"),
                                               (member_node, dimension_node, "related_to", "member_of")):
            aid = "semantic_reference:" + digest([data.snapshot_id, predicate, left["id"], right["id"]])[:24]
            assertions[aid] = {"id": aid, "subject": left["id"], "object": right["id"],
                "predicate": parent, "parent_relation": parent, "predicate_name": predicate,
                "instance_scope": "source_bound_lexical_member_path_not_type_identity",
                "definition": canonical_relation_definition(parent, predicate),
                "evidence_scope": "verified_lexical_member_path", "proof": proof,
                "evidence_ids": evidence, "decision": {"status": "accepted", "method": "replayed_source_path"}}
            edge_ids.append(aid)
        rid = canonical_relation_id("related_to", term_type.id, dimension.id, predicate_name="term_dimension_context")
        path = {"id": "semantic_reference_path:" + digest([term_node["id"], member_node["id"], dimension_node["id"]])[:24],
                "relation_type_id": rid, "source_type_id": term_type.id, "target_type_id": dimension.id,
                "instance_ids": [term_node["id"], member_node["id"], dimension_node["id"]],
                "assertion_ids": edge_ids, "proof": proof, "evidence_ids": evidence}
        # Self-contained evidence lets ordinary validate_plan replay a type
        # relation after reload, without a transient stage-result side channel.
        path_evidence_id = "lexical_member_path:" + digest([data.snapshot_id, path["id"]])[:24]
        data.evidence[path_evidence_id] = {
            "id": path_evidence_id, "origin": "semantic_reference_path",
            "raw_fragment": "Term denotes member; member belongs to Dimension",
            "raw_fragment_truncated": False, "source_ref": {"snapshot_id": data.snapshot_id},
            "path": path, "instances": [term_node, member_node, dimension_node],
            "assertions": [assertions[aid] for aid in edge_ids]}
        path["path_evidence_id"] = path_evidence_id
        path["evidence_ids"] = [*evidence, path_evidence_id]
        paths.append(path)
        relation_proofs[rid].append(path)
    existing = {item.id: item for item in core.relation_types}
    for rid, group in relation_proofs.items():
        core.relation_types = [item for item in core.relation_types if item.id != rid]
        relation = DerivedType(id=rid, parent="related_to", category="business_relation_type",
            label="term_dimension_context", predicate_name="term_dimension_context",
            definition=canonical_relation_definition("related_to", "term_dimension_context"),
            domain=[group[0]["source_type_id"]], range=[group[0]["target_type_id"]],
            endpoint_basis="semantic_reference", evidence_scope="verified_lexical_member_path",
            evidence_ids=sorted({eid for path in group for eid in path["evidence_ids"]} |
                                set(existing[rid].evidence_ids if rid in existing else [])))
        core.relation_types.append(relation)
    result = {"plan": core, "json_reference_candidates": candidates, "checked_reference_rules": checks,
              "instances": list(nodes.values()), "assertions": list(assertions.values()), "paths": paths,
              "relation_contracts": [{"relation_type_id": rid, "path_ids": [path["id"] for path in group],
                  "endpoint_semantics": "Term denotes member; member belongs to Dimension; no type identity claim"}
                 for rid, group in relation_proofs.items()], "pending": pending,
              "coverage": {"accepted_term_source_records": len(terms), "paths_accepted": len(paths),
                  "unique_member_instances": sum(node["instance_kind"] == "dimension_member" for node in nodes.values()),
                  "relation_types": len(relation_proofs), "assertions": len(assertions),
                  "pending": len(pending), "source_rows_not_examined": omissions,
                  "conflicting_source_type_alignments": len(conflicts), "discovery": recalled["coverage"],
                  "member_tables_with_supported_declaration": len(recognized),
                  "tables_without_supported_member_declaration": skipped_member_tables,
                  "completeness_scope": "accepted_Term_sources_and_source_declared_member_namespaces_only",
                  "llm_calls": 0, "partial": bool(pending or omissions or recalled["coverage"]["partial"])}}
    for assertion in result["assertions"]:
        verify_semantic_reference_assertion(data, assertion, nodes, core)
    result["reference_plans"] = [RelationPlan(
        id="plan:" + digest([path["id"], "denotes"] )[:24],
        source_table=path["proof"]["candidate"]["source"]["table"],
        source_column=path["proof"]["candidate"]["source"]["field"],
        source_path=path["proof"]["candidate"]["source"]["source_path"],
        target_table=path["proof"]["candidate"]["target"]["table"],
        target_column=path["proof"]["candidate"]["target"]["field"], mode="identifier",
        predicate="points_to", witness_snapshot_id=data.snapshot_id,
        witnessed_pairs=[WitnessedPair(source_record_id=nodes[path["instance_ids"][0]]["source_ref"]["record_id"],
                                      target_record_id=nodes[path["instance_ids"][1]]["source_ref"]["record_id"])],
        evidence_ids=path["evidence_ids"]).model_dump()
        for path in paths if len(path["proof"]["member_lookup_bindings"]) == 1]
    return result


def verify_semantic_reference_assertion(data, assertion, instances, plan):
    """Re-read every proof cell and execute the two referenced keys before export."""
    nodes = instances if isinstance(instances, dict) else {item["id"]: item for item in instances}
    types = {item.id: item for item in plan.object_types}
    proof = assertion["proof"]
    if proof.get("snapshot_id") != data.snapshot_id or assertion.get("decision", {}).get("status") != "accepted":
        raise ValueError("Semantic reference has no current accepted proof")
    term, member, dimension = (nodes[proof[key]] for key in ("term_node", "member_node", "dimension_node"))
    if (types[term["ontology_type_id"]].parent != "Term"
            or types[dimension["ontology_type_id"]].parent != "Dimension"
            or member["ontology_type_id"] != dimension["ontology_type_id"]):
        raise ValueError("Semantic reference levels or accepted types changed")
    rows = []
    for node in (term, member, dimension):
        ref = node["source_ref"]
        row = _row(data, ref["table"], ref["row"])
        if data.record_id(ref["table"], row) != ref["record_id"]:
            raise ValueError("Semantic reference source identity changed")
        rows.append(row)
    for eid in proof["evidence_ids"]:
        evidence = data.evidence.get(eid) or {}
        if evidence.get("origin") != "observed_record":
            continue
        ref = evidence.get("source_ref") or {}
        if (ref.get("snapshot_id") != data.snapshot_id or evidence.get("raw_fragment_truncated")
                or _row(data, ref["table"], ref["row"]).get(ref["column"]) != evidence.get("raw_fragment")):
            raise ValueError("Semantic reference source evidence changed")
    source = proof["candidate"]["source"]
    leaves = {tuple(path): value for path, value in json_scalar_paths(rows[0].get(source["field"]))}
    spec = proof["member_spec"]
    if (source["table"] != term["source_ref"]["table"]
            or proof["candidate"]["target"] != {"table": spec["table"], "field": spec["field"]}
            or spec["table"] != member["source_ref"]["table"]):
        raise ValueError("Semantic reference plan endpoints changed")
    cells = [(term, proof["source_lexical_column"]), (term, source["field"]),
             (member, spec["field"]), (member, proof["member_lexical_column"]),
             (member, spec["scope_column"]), (member, proof["membership_text_column"]),
             (dimension, proof["dimension_key_column"])]
    required = {"record:" + digest([data.snapshot_id, node["source_ref"]["record_id"], column])[:24]
                for node, column in cells}
    if not required <= set(proof["evidence_ids"]):
        raise ValueError("Semantic reference omitted original source cell evidence")
    if proof["source_lexical_column"] not in {column["column_name"] for column in _lexical_columns(data, source["table"])}:
        raise ValueError("Semantic reference source field is not lexical")
    if leaves.get(tuple(source["source_path"])) != rows[1].get(spec["field"]):
        raise ValueError("Semantic reference JSON path no longer names its member")
    if spec not in _member_specs(data):
        raise ValueError("Semantic reference member namespace declaration changed")
    bindings = [[spec["field"], leaves[tuple(source["source_path"])]]]
    namespace = json_member_scope(leaves, source["source_path"], spec["scope_column"])
    if namespace:
        bindings.append([spec["scope_column"], namespace["value"]])
    if namespace != proof.get("member_namespace_binding"):
        raise ValueError("Semantic reference namespace path lacks same-object evidence")
    if bindings != [list(item) for item in proof["member_lookup_bindings"]]:
        raise ValueError("Semantic reference scope binding lacks original JSON evidence")
    matched = _lookup(data, spec["table"], proof["member_lookup_bindings"])
    if len(matched) != 1 or data.record_id(spec["table"], matched[0]) != member["source_ref"]["record_id"]:
        raise ValueError("Semantic reference member key is ambiguous")
    if (_normal(rows[0].get(proof["source_lexical_column"], "")) != _normal(term["label"])
            or _lexical_match(data, term["label"], spec["table"], rows[1]) is None
            or _membership_text(data, spec["table"], rows[1], dimension["label"]) is None
            or rows[1].get(spec["scope_column"]) != rows[2].get(proof["dimension_key_column"])):
        raise ValueError("Semantic reference lexical or membership evidence changed")
    if (_identity_conflict(data, source["table"], rows[0], spec["table"], rows[1])
            or _identity_conflict(data, spec["table"], rows[1], dimension["source_ref"]["table"], rows[2])):
        raise ValueError("Semantic reference identity scope changed")
    dimension_table = dimension["source_ref"]["table"]
    if proof["dimension_key_column"] not in {
            column["column_name"] for column in _columns(data, dimension_table)
            if _CODE.search(str(column.get("column_comment") or ""))}:
        raise ValueError("Semantic reference Dimension key lacks a source declaration")
    dimension_matches = _lookup(data, dimension_table,
        [(proof["dimension_key_column"], rows[2][proof["dimension_key_column"]])])
    if (len(dimension_matches) != 1 or data.record_id(dimension_table, dimension_matches[0])
            != dimension["source_ref"]["record_id"]):
        raise ValueError("Semantic reference Dimension key became ambiguous")
    expected = {"denotes": (term["id"], member["id"], "points_to"),
                "member_of": (member["id"], dimension["id"], "related_to")}
    if expected.get(assertion.get("predicate_name")) != (assertion.get("subject"), assertion.get("object"), assertion.get("parent_relation")):
        raise ValueError("Semantic reference predicate or direction changed")
    if assertion.get("predicate") != assertion.get("parent_relation"):
        raise ValueError("Semantic reference executable predicate changed")


def verify_semantic_reference_relation(data, relation, plan):
    """Validate from persisted data.evidence; no in-memory stage result is needed."""
    proofs = [data.evidence[eid] for eid in relation.evidence_ids if eid in data.evidence
              and data.evidence[eid].get("origin") == "semantic_reference_path"]
    if (not proofs or relation.predicate_name != "term_dimension_context" or relation.parent != "related_to"
            or relation.endpoint_basis != "semantic_reference"
            or relation.evidence_scope != "verified_lexical_member_path"):
        raise ValueError("Semantic reference type relation lacks a witnessed path")
    for evidence in proofs:
        path = evidence["path"]
        if path["relation_type_id"] != relation.id:
            raise ValueError("Semantic reference path names a different relation")
        if relation.domain != [path["source_type_id"]] or relation.range != [path["target_type_id"]]:
            raise ValueError("Semantic reference type endpoints differ from the path")
        if not set(path["evidence_ids"]) <= set(relation.evidence_ids):
            raise ValueError("Semantic reference relation dropped path evidence")
        assertions = {item["id"]: item for item in evidence["assertions"]}
        for aid in path["assertion_ids"]:
            verify_semantic_reference_assertion(data, assertions[aid], evidence["instances"], plan)
