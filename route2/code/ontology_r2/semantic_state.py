"""Resume accepted semantic work; raw data and changed contracts are rechecked.

The checkpoint is deliberately snapshot-bound. New observation values may use
an exported mapping, but cannot silently inherit old evidence identifiers.
"""
from pathlib import Path
import json
import os

from .models import BuildPlan
from .storage import digest
from .validation import validate_plan


def state_contract(data, profile, *, implementation_code_hash):
    # Use the exact package hash recorded by pipeline.build, not a second hash.
    if (not isinstance(implementation_code_hash, str)
            or len(implementation_code_hash) != 64
            or any(char not in "0123456789abcdef" for char in implementation_code_hash)):
        raise ValueError("implementation_code_hash must be the pipeline SHA-256 hash")
    from .llm import SYSTEM, TASK_PROMPTS, TEMPLATE_INDUCTION_PROMPT
    # Definition parameters now participate in type identity and relation
    # compatibility; pre-contract checkpoints must not bypass these checks.
    return {"version": 4, "snapshot_id": data.snapshot_id,
            "implementation_code_hash": implementation_code_hash,
            "retrieval_contract": getattr(data, "semantic_retrieval_contract", {}),
            "profile_hash": digest(profile), "prompts_hash": digest([SYSTEM, TASK_PROMPTS, TEMPLATE_INDUCTION_PROMPT]),
            # Source files alone do not capture a changed exclusion policy.
            # Replaying old evidence must not undo a newly excluded column.
            "semantic_exclusions": {name: sorted(table.get("semantic_excluded_columns", []))
                                    for name, table in sorted(data.tables.items())
                                    if table.get("semantic_excluded_columns")},
            "model": os.getenv("ONTOLOGY_LLM_MODEL", "mock")}


def restore_state(path, data, profile, *, implementation_code_hash):
    expected_contract = state_contract(
        data, profile, implementation_code_hash=implementation_code_hash)
    if not path:
        return None
    path = Path(path)
    if path.is_dir():
        path /= "semantic_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    contract = state.get("contract")
    # Reject legacy or differently compiled acceptances before mutating data.
    if not isinstance(contract, dict) or not contract.get("implementation_code_hash"):
        raise ValueError("Semantic state lacks implementation_code_hash; legacy checkpoints cannot be resumed")
    if contract["implementation_code_hash"] != implementation_code_hash:
        raise ValueError("Semantic state implementation_code_hash differs from the current implementation")
    if contract != expected_contract:
        raise ValueError("Semantic state differs from the input snapshot, model or semantic contract")
    role_report = state.get("column_role_report")
    if role_report is not None and (not isinstance(role_report, dict)
                                   or role_report.get("source_snapshot") != data.snapshot_id):
        raise ValueError("Column-role report differs from the checkpoint snapshot")
    data.evidence.update(state.get("evidence", {}))
    for name, roles in state.get("column_roles", {}).items():
        if name in data.tables:
            data.tables[name]["inferred_semantic_roles"] = roles
    state["plan"] = BuildPlan.model_validate(state["plan"])
    errors = validate_plan(state["plan"], data, profile)
    if errors:
        raise ValueError("Invalid resumed semantic state: " + "; ".join(errors))
    # Preserve the original completeness accounting. The caller decides
    # whether to reuse it or continue inference for unresolved columns.
    if role_report is not None:
        data.column_role_report = role_report
    return state


def save_state(path, data, profile, result, *, implementation_code_hash):
    contract = state_contract(data, profile, implementation_code_hash=implementation_code_hash)
    path = Path(path)
    state = {key: value for key, value in result.items() if key != "plan"}
    state.update(contract=contract,
                 plan=result["plan"].model_dump(), evidence=data.evidence,
                 column_roles={name: table.get("inferred_semantic_roles", [])
                               for name, table in data.tables.items()})
    role_report = getattr(data, "column_role_report", None)
    if role_report is not None:
        state["column_role_report"] = role_report
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
