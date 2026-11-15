"""Rendered by l9-semantic-compiler (pytest/v2): evaluator copied from passes/fixture_ir.py."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

OPERAND_FREE = frozenset({"is_null", "is_present", "is_absent"})
_MISSING = object()
_CLAUSE_PREDICATES = frozenset(
    {"equals", "not_equals", "includes", "excludes", "greater_than", "less_than"}
) | OPERAND_FREE


def value_references(value: Any) -> list[tuple[str, Any]]:
    """Every ``{"$name": ...}`` single-key mapping anywhere inside *value*."""
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        keys = list(value)
        if len(keys) == 1 and isinstance(keys[0], str) and keys[0].startswith("$"):
            found.append((keys[0], value[keys[0]]))
        else:
            for child in value.values():
                found.extend(value_references(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(value_references(child))
    return found


def resolve_path(value: Any, path: str) -> Any:
    """The value at a dotted *path* (``""`` is the whole value), or a missing sentinel."""
    if path == "":
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


def count_where_operand_problem(operand: Any) -> str | None:
    """Why a count_where operand is not ``{where: [{path, predicate, operand?}, ...], count}``, or None."""
    if not isinstance(operand, Mapping) or set(operand) != {"where", "count"}:
        return "a count_where operand is exactly {where, count}"
    count, where = operand["count"], operand["where"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return "count is a non-negative integer"
    if not isinstance(where, list) or not where:
        return "where is a non-empty list of clauses"
    for clause in where:
        if not isinstance(clause, Mapping) or not isinstance(clause.get("path"), str):
            return "a where clause names a dotted path"
        predicate = clause.get("predicate")
        if predicate not in _CLAUSE_PREDICATES:
            return f"a where clause predicate is a scalar predicate, not {predicate!r}"
        if set(clause) - {"path", "predicate", "operand"}:
            return "a where clause is {path, predicate, operand?}"
        if (predicate in OPERAND_FREE) == ("operand" in clause):
            return f"{predicate} takes {'no' if predicate in OPERAND_FREE else 'a literal'} operand"
        if value_references(clause.get("operand")):
            return "a where clause operand is a literal, never $from or $observed"
    return None


def _scalar(predicate: str, value: Any, operand: Any) -> bool:
    """One scalar predicate over a resolved value; raises on an unknown predicate or incomparable values."""
    if predicate == "equals":
        return bool(value == operand)
    if predicate == "not_equals":
        return bool(value != operand)
    if predicate == "greater_than":
        return bool(value > operand)
    if predicate == "less_than":
        return bool(value < operand)
    if predicate == "includes":
        return operand in value
    if predicate == "excludes":
        return operand not in value
    raise ValueError(predicate)


def _clause_holds(clause: Mapping[str, Any], member: Any) -> bool:
    """One count_where clause over one group member. Presence predicates inspect absence; any other
    clause whose path is absent, or whose values do not compare, is not satisfied."""
    value = resolve_path(member, str(clause.get("path", "")))
    predicate = clause.get("predicate")
    if predicate == "is_absent":
        return value is _MISSING
    if predicate == "is_present":
        return value is not _MISSING
    if predicate == "is_null":
        return value is not _MISSING and value is None
    if value is _MISSING:
        return False
    try:
        return _scalar(str(predicate), value, clause.get("operand"))
    except (TypeError, ValueError):
        return False


def evaluate_count_where(assertion: Mapping[str, Any], group: Sequence[Any]) -> str:
    """``pass`` | ``fail`` | ``error``: holds iff exactly ``count`` members satisfy every clause."""
    operand = assertion.get("operand")
    if count_where_operand_problem(operand) is not None:
        return "error"
    assert isinstance(operand, Mapping)
    satisfied = sum(
        1 for member in group if all(_clause_holds(c, member) for c in operand["where"])
    )
    return "pass" if satisfied == operand["count"] else "fail"


def evaluate_precondition(precondition: Mapping[str, Any], result: Any) -> str:
    """``hold`` | ``error``: a precondition that does not hold is a setup error, never fail or pass."""
    for path, literal in precondition.items():
        value = resolve_path(result, str(path))
        if value is _MISSING or value != literal:
            return "error"
    return "hold"


def evaluate_assertion(
    assertion: Mapping[str, Any],
    recorded: Mapping[str, Any],
    group: Sequence[Any] | None = None,
) -> str:
    """``pass`` | ``fail`` | ``error`` for one assertion over recorded results.

    ``recorded`` maps a step or observation id to its recorded result; ``group`` is the when group's
    member results, used only by ``count_where``. ``is_present``, ``is_absent`` and ``is_null``
    inspect absence and are never an error for it; a relational target or ``$observed`` path absent
    from the recorded result is ``error``, never ``pass``.
    """
    target = assertion["target"]
    predicate = assertion["predicate"]
    if predicate == "count_where":
        if target.get("kind") != "group" or group is None:
            return "error"
        return evaluate_count_where(assertion, group)
    if target.get("ref") not in recorded:
        return "error"
    value = resolve_path(recorded[target["ref"]], target.get("path", ""))
    if predicate == "is_absent":
        return "pass" if value is _MISSING else "fail"
    if predicate == "is_present":
        return "fail" if value is _MISSING else "pass"
    if predicate == "is_null":
        return "pass" if value is not _MISSING and value is None else "fail"
    if value is _MISSING:
        return "error"
    operand = assertion.get("operand")
    refs = value_references(operand)
    if refs:
        name, ref = refs[0]
        if name != "$observed" or not isinstance(ref, Mapping):
            return "error"
        step = ref.get("step")
        source = (
            recorded[step] if isinstance(step, str) and step in recorded else _MISSING
        )
        operand = (
            _MISSING
            if source is _MISSING
            else resolve_path(source, ref.get("path", ""))
        )
        if operand is _MISSING:
            return "error"
    try:
        held = _scalar(predicate, value, operand)
    except (TypeError, ValueError):
        return "error"
    return "pass" if held else "fail"
