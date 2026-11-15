from __future__ import annotations

from typing import Any

COLLECTIONS = {
    "objects": "state_objects",
    "revisions": "state_revisions",
    "operations": "state_operations",
    "events": "state_events",
    "claims": "state_claims",
    "consumer_cursors": "state_consumer_cursors",
    "scope_counters": "state_scope_counters",
}

# (collection key, index fields, unique, name)
REQUIRED_INDEXES: tuple[tuple[str, tuple[tuple[str, int], ...], bool, str], ...] = (
    (
        "objects",
        (("tenant_org_id", 1), ("scope_ref", 1), ("object_id", 1)),
        True,
        "uq_state_key",
    ),
    (
        "revisions",
        (("tenant_org_id", 1), ("scope_ref", 1), ("object_id", 1), ("revision", 1)),
        True,
        "uq_state_revision",
    ),
    (
        "operations",
        (("tenant_org_id", 1), ("scope_ref", 1), ("operation_id", 1)),
        True,
        "uq_state_operation",
    ),
    (
        "events",
        (("tenant_org_id", 1), ("scope_ref", 1), ("journal_seq", 1)),
        True,
        "uq_scope_journal_seq",
    ),
    ("events", (("event_id", 1),), True, "uq_event_id"),
    (
        "claims",
        (("tenant_org_id", 1), ("scope_ref", 1), ("object_id", 1)),
        True,
        "uq_state_claim_lineage",
    ),
    (
        "consumer_cursors",
        (("tenant_org_id", 1), ("scope_ref", 1), ("consumer_ref", 1)),
        True,
        "uq_consumer_cursor",
    ),
    (
        "scope_counters",
        (("tenant_org_id", 1), ("scope_ref", 1)),
        True,
        "uq_scope_counter",
    ),
)


def state_key(tenant_org_id: str, scope_ref: str, object_id: str) -> dict[str, Any]:
    return {
        "tenant_org_id": tenant_org_id,
        "scope_ref": scope_ref,
        "object_id": object_id,
    }


def operation_key(tenant_org_id: str, scope_ref: str, operation_id: str) -> dict[str, Any]:
    return {
        "tenant_org_id": tenant_org_id,
        "scope_ref": scope_ref,
        "operation_id": operation_id,
    }


def inactive_or_expired_expr() -> dict[str, Any]:
    return {
        "$or": [
            {"$ne": ["$active", True]},
            {"$lte": ["$expires_at", "$$NOW"]},
        ]
    }


def active_unexpired_expr() -> dict[str, Any]:
    return {
        "$and": [
            {"$eq": ["$active", True]},
            {"$gt": ["$expires_at", "$$NOW"]},
        ]
    }


def acquire_claim_filter(tenant_org_id: str, scope_ref: str, object_id: str) -> dict[str, Any]:
    return {
        **state_key(tenant_org_id, scope_ref, object_id),
        "$expr": inactive_or_expired_expr(),
    }


def exact_claim_filter(
    tenant_org_id: str,
    scope_ref: str,
    object_id: str,
    *,
    claim_id: str,
    fencing_token: int,
    holder_ref: str,
) -> dict[str, Any]:
    return {
        **state_key(tenant_org_id, scope_ref, object_id),
        "claim_id": claim_id,
        "fencing_token": fencing_token,
        "holder_ref": holder_ref,
        "$expr": active_unexpired_expr(),
    }


def exact_claim_fence_filter(
    tenant_org_id: str,
    scope_ref: str,
    object_id: str,
    *,
    claim_id: str,
    fencing_token: int,
) -> dict[str, Any]:
    """Exact active claim/fence guard for retention-authorized hard erase."""
    return {
        **state_key(tenant_org_id, scope_ref, object_id),
        "claim_id": claim_id,
        "fencing_token": fencing_token,
        "$expr": active_unexpired_expr(),
    }


def claimless_transition_guard_filter(
    tenant_org_id: str, scope_ref: str, object_id: str
) -> dict[str, Any]:
    return {
        **state_key(tenant_org_id, scope_ref, object_id),
        "$expr": inactive_or_expired_expr(),
    }


def acquire_claim_pipeline(
    *, claim_id: str, holder_ref: str, ttl_seconds: int
) -> list[dict[str, Any]]:
    ttl_ms = ttl_seconds * 1000
    next_fence = {"$add": [{"$ifNull": ["$highest_fence", 0]}, 1]}
    return [
        {
            "$set": {
                "highest_fence": next_fence,
                "fencing_token": next_fence,
                "active": True,
                "claim_id": claim_id,
                "holder_ref": holder_ref,
                "acquired_at": "$$NOW",
                "expires_at": {"$add": ["$$NOW", ttl_ms]},
                "released_at": None,
            }
        }
    ]


def renew_claim_pipeline(ttl_seconds: int) -> list[dict[str, Any]]:
    return [
        {
            "$set": {
                "expires_at": {"$add": ["$$NOW", ttl_seconds * 1000]},
                "renewed_at": "$$NOW",
                "released_at": None,
            }
        }
    ]


def release_claim_pipeline() -> list[dict[str, Any]]:
    return [{"$set": {"active": False, "released_at": "$$NOW"}}]


def transition_guard_pipeline() -> list[dict[str, Any]]:
    return [
        {
            "$set": {
                "guard_version": {"$add": [{"$ifNull": ["$guard_version", 0]}, 1]},
                "guarded_at": "$$NOW",
            }
        }
    ]
