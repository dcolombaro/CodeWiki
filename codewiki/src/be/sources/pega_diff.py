"""Compare verified Pega evidence packages and identify affected modules."""

from __future__ import annotations

import json
from typing import Any

from .evidence import stable_json


def _indexed(rows: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_id = str(row.get("id") or "")
        if not row_id or row_id in indexed:
            raise ValueError(f"Missing or duplicate {label} ID in evidence package")
        indexed[row_id] = row
    return indexed


def _normalized_properties(properties: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in properties.items() if key != "projection_token"}
    qualifiers = result.get("qualifiers_json")
    if isinstance(qualifiers, str) and qualifiers:
        result["qualifiers_json"] = json.loads(qualifiers)
    return result


def _entity_signature(row: dict[str, Any]) -> str:
    return stable_json(
        {
            key: value for key, value in row.items() if key != "properties"
        }
        | {"properties": _normalized_properties(row.get("properties") or {})}
    )


def _relationship_signature(row: dict[str, Any]) -> str:
    return stable_json(
        {
            key: value for key, value in row.items() if key != "properties"
        }
        | {"properties": _normalized_properties(row.get("properties") or {})}
    )


def _document_signature(row: dict[str, Any]) -> str:
    return stable_json(
        {key: value for key, value in row.items() if key not in {"cache_path", "metadata_sha256"}}
    )


def _difference(
    before: dict[str, Any], after: dict[str, Any], signature
) -> dict[str, list[str]]:
    before_ids = set(before)
    after_ids = set(after)
    common = before_ids & after_ids
    return {
        "added": sorted(after_ids - before_ids),
        "removed": sorted(before_ids - after_ids),
        "changed": sorted(
            item_id for item_id in common if signature(before[item_id]) != signature(after[item_id])
        ),
    }


def _scope_selection(package: dict[str, Any]) -> dict[str, Any]:
    scope = package["scope"]
    return {
        key: scope.get(key)
        for key in (
            "mode", "seed_entity_ids", "depth", "relationship_types",
            "expanded_entity_ids", "expansion_depth", "discovery",
        )
    }


def compare_evidence_packages(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    before_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Report source changes and impacted pre-existing module owners.

    This is evidence-diff bookkeeping, not proof that generated prose is fresh.
    A changed selection scope is flagged separately from an upstream update.
    """
    if before.get("project_id") != after.get("project_id"):
        raise ValueError("Pega snapshots belong to different projects")
    if before_plan is not None and before_plan.get("snapshot_key") != before.get("snapshot_key"):
        raise ValueError("The module plan does not belong to the before snapshot")

    before_entities = _indexed(before["entities"], "entity")
    after_entities = _indexed(after["entities"], "entity")
    before_edges = _indexed(before["relationships"], "relationship")
    after_edges = _indexed(after["relationships"], "relationship")
    entity_changes = _difference(before_entities, after_entities, _entity_signature)
    relationship_changes = _difference(before_edges, after_edges, _relationship_signature)
    document_changes = _difference(
        before["documents"],
        after["documents"],
        _document_signature,
    )
    for document_id in before["documents"].keys() & after["documents"].keys():
        before_metadata_hash = before["documents"][document_id].get("metadata_sha256")
        after_metadata_hash = after["documents"][document_id].get("metadata_sha256")
        if (
            before_metadata_hash
            and after_metadata_hash
            and before_metadata_hash != after_metadata_hash
        ):
            document_changes["changed"].append(document_id)
    document_changes["changed"] = sorted(set(document_changes["changed"]))

    affected_entities = set().union(*entity_changes.values())
    for document_id in set().union(*document_changes.values()):
        for manifest in (before["documents"], after["documents"]):
            if document_id in manifest:
                affected_entities.update(manifest[document_id].get("entity_ids") or [])
    for relation_id in set().union(*relationship_changes.values()):
        for edges in (before_edges, after_edges):
            relation = edges.get(relation_id)
            if relation:
                affected_entities.update(
                    (relation["source_entity_id"], relation["target_entity_id"])
                )

    owner_map = (before_plan or {}).get("primary_owner") or {}
    impacted_modules = sorted(
        {owner_map[entity_id] for entity_id in affected_entities if entity_id in owner_map}
    )
    selection_changed = _scope_selection(before) != _scope_selection(after)
    source_metadata_changed = any(
        before.get(key) != after.get(key) for key in ("status", "schema")
    )
    evidence_changed = any(
        changes[category]
        for changes in (entity_changes, relationship_changes, document_changes)
        for category in ("added", "removed", "changed")
    )
    return {
        "source_kind": "pega",
        "project_id": before["project_id"],
        "before_snapshot_key": before["snapshot_key"],
        "after_snapshot_key": after["snapshot_key"],
        "same_selection_scope": not selection_changed,
        "comparison_kind": "evidence_refresh" if not selection_changed else "scope_change",
        "evidence_changed": evidence_changed,
        "source_metadata_changed": source_metadata_changed,
        "document_metadata_hash_coverage": {
            "before": sum(
                bool(row.get("metadata_sha256")) for row in before["documents"].values()
            ),
            "after": sum(
                bool(row.get("metadata_sha256")) for row in after["documents"].values()
            ),
        },
        "changes": {
            "entities": entity_changes,
            "relationships": relationship_changes,
            "documents": document_changes,
        },
        "affected_entity_ids": sorted(affected_entities),
        "impact_from_before_plan": {
            "module_names": impacted_modules,
            "unassigned_entity_ids": sorted(affected_entities - owner_map.keys()),
            "overview_needs_review": bool(impacted_modules or selection_changed),
        },
    }
