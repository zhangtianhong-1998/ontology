"""Small, strict contracts shared by model calls and deterministic execution."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Condition(Strict):
    op: Literal["eq", "in", "and", "or", "range"]
    field: str | None = None
    value: str | None = None
    values: list[str] = Field(default_factory=list)
    children: list["Condition"] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_operands(self):
        if self.op in ("and", "or"):
            if not self.children or self.field is not None or self.value is not None or self.values:
                raise ValueError("boolean condition requires children only")
        elif not self.field or self.children:
            raise ValueError("comparison requires a field and no children")
        elif self.op == "eq" and (self.value is None or self.values):
            raise ValueError("eq requires one value")
        elif self.op in ("in", "range") and self.value is not None:
            raise ValueError("in/range requires values, not value")
        if self.op == "range":
            from decimal import Decimal, InvalidOperation
            try:
                if len(self.values) != 2:
                    raise ValueError("range requires two bounds")
                lo, hi = map(Decimal, self.values)
                if not lo.is_finite() or not hi.is_finite() or lo > hi:
                    raise ValueError("range requires ordered finite bounds")
            except InvalidOperation as exc:
                raise ValueError("invalid decimal bound") from exc
        return self


class DerivedType(Strict):
    id: str
    parent: str
    definition: str
    evidence_ids: list[str]


class TablePlan(Strict):
    table: str
    object_type: str
    label_column: str | None = None
    identity_columns: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)
    evidence_ids: list[str]


class RelationPlan(Strict):
    id: str
    source_table: str
    target_table: str
    mode: Literal["identifier", "formula", "text", "members"]
    source_column: str
    source_path: list[str] = Field(default_factory=list)
    target_column: str
    scope_bindings: dict[str, str] = Field(default_factory=dict)
    selector: Condition | None = None
    predicate: str = "points_to"
    delimiter: str | None = None
    semantics: Literal["reference", "observed_member", "allowed_member"] = "reference"
    context_columns: list[str] = Field(default_factory=list)
    evidence_ids: list[str]


class BuildPlan(Strict):
    object_types: list[DerivedType] = Field(default_factory=list)
    relation_types: list[DerivedType] = Field(default_factory=list)
    tables: list[TablePlan] = Field(default_factory=list)
    relations: list[RelationPlan] = Field(default_factory=list)
    knowledge_questions: list[str] = Field(default_factory=list)


class Review(Strict):
    accepted: bool
    errors: list[str] = Field(default_factory=list)
    corrected_delta: BuildPlan | None = None


class LinkDecision(Strict):
    target_record_id: str | None = None
    status: Literal["accepted", "unresolved", "conflict"]
    source_quote: str = ""
    target_quote: str = ""
    explanation: str = ""


class KnowledgeClaim(Strict):
    document_id: str
    quote: str
    statement: str
    scope: str
    polarity: Literal["supports", "contradicts"] = "supports"
    source_evidence_ids: list[str] = Field(default_factory=list)
    contribution: Literal["definition", "relationship", "scope", "terminology", "conflict"] = "relationship"


class KnowledgeSummary(Strict):
    claims: list[KnowledgeClaim] = Field(default_factory=list)
    remaining_gaps: list[str] = Field(default_factory=list)
    stop_reason: str


class AlignmentDecision(Strict):
    external_uri: str | None = None
    mapping_kind: Literal["exact", "broader", "narrower", "related", "unmapped"]
    explanation: str
    internal_quote: str = ""
    external_quote: str = ""


class ExternalQueries(Strict):
    queries: list[str] = Field(default_factory=list, max_length=4)


def evaluate(condition: Condition | None, row: dict[str, Any]) -> bool | None:
    """Three-valued selection: a missing/null operand never becomes true."""
    if condition is None:
        return True
    if condition.op in ("and", "or"):
        if not condition.children:
            raise ValueError("Empty boolean condition")
        vals = [evaluate(c, row) for c in condition.children]
        if condition.op == "and":
            return False if False in vals else None if None in vals else True
        return True if True in vals else None if None in vals else False
    value = row.get(condition.field)
    if value is None or value == "":
        return None
    if condition.op == "eq":
        return value == condition.value
    if condition.op == "in":
        return value in condition.values
    from decimal import Decimal, InvalidOperation
    try:
        if len(condition.values) != 2:
            raise ValueError("range needs two decimal bounds")
        lo, hi = map(Decimal, condition.values)
        return lo <= Decimal(value) <= hi
    except (InvalidOperation, TypeError):
        return None
