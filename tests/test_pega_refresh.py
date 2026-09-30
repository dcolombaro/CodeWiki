"""Tests for the automatic PEGA evidence refresh/cache path."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from codewiki.src.be.pega_refresh import (
    PegaEvidenceStore,
    _cached_revision_matches,
    prepare_pega_evidence,
    scope_request,
    slice_project_package,
)
from codewiki.src.be.sources.evidence import digest, stable_json


def _entity(project: str, short_id: str, name: str) -> dict[str, Any]:
    entity_id = f"{project}::pega:{short_id}"
    properties = {
        "id": entity_id,
        "project_id": project,
        "name": name,
        "entity_type": "PegaActivity",
        "rule_type": "Rule-Obj-Activity",
        "rule_category": "activity",
        "class_name": "Example-Class",
        "ruleset": "Example",
        "is_external": False,
        "is_embedded": False,
        "projection_token": "row-token",
    }
    return {
        "id": entity_id,
        "name": name,
        "entity_type": "PegaActivity",
        "rule_type": "Rule-Obj-Activity",
        "rule_category": "activity",
        "class_name": "Example-Class",
        "ruleset": "Example",
        "is_external": False,
        "is_embedded": False,
        "document_ids": [],
        "properties": properties,
    }


def _project_package(project: str = "example") -> dict[str, Any]:
    package = {
        "source_kind": "pega",
        "project_id": project,
        "scope": {
            "mode": "project",
            "entity_count": 1,
            "relationship_count": 0,
            "document_count": 0,
            "complete_for_requested_scope": True,
        },
        "status": {
            "project_id": project,
            "entities": 1,
            "documents": 0,
            "relationships": 0,
            "projection_manifest_hash": "manifest-v1",
            "projection_status": "ready",
        },
        "schema": {"relationship_shapes": []},
        "entities": [_entity(project, "one", "ExampleActivity")],
        "relationships": [],
        "documents": {},
        "unresolved_references": [],
        "requests": [{"tool": "old_capture"}],
    }
    package["snapshot_key"] = digest(
        stable_json({key: value for key, value in package.items() if key != "requests"})
    )
    return package


class _RevisionClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(name)
        if name == "list_projects":
            return {"projects": [{"project_id": "example"}]}
        if name == "kb_status":
            return {
                "project_id": "example",
                "entities": 1,
                "documents": 0,
                "relationships": 0,
            }
        if name == "read_cypher":
            return {
                "rows": [{
                    "projection_manifest_hash": "manifest-v1",
                    "projection_status": "ready",
                }]
            }
        raise AssertionError(f"Unexpected MCP retrieval on cache hit: {name} {arguments}")


def test_matching_project_revision_reuses_package_without_graph_or_document_reads(
    tmp_path: Path,
) -> None:
    store = PegaEvidenceStore(tmp_path / "cache", "example")
    request = scope_request()
    store.save(request, _project_package())
    client = _RevisionClient()

    provider, package, decision = asyncio.run(
        prepare_pega_evidence(
            project="example",
            client=client,  # type: ignore[arg-type]
            source_root=tmp_path,
            store=store,
            request=request,
        )
    )

    assert decision["decision"] == "reused_scope_package"
    assert package["snapshot_key"] == _project_package()["snapshot_key"]
    assert package["requests"] == []
    assert provider.transport is client
    assert client.calls == ["list_projects", "kb_status", "read_cypher"]


def test_cached_revision_requires_manifest_hash_and_project_counts() -> None:
    package = _project_package()
    live_status = dict(package["status"])

    assert _cached_revision_matches(package, "manifest-v1", live_status)
    assert not _cached_revision_matches(package, "manifest-v2", live_status)
    assert not _cached_revision_matches(package, "manifest-v1", live_status | {"entities": 2})


def test_focused_slice_can_be_derived_from_complete_cached_project() -> None:
    project = "example"
    entities = [_entity(project, key, name) for key, name in zip("abcd", "ABCD")]
    relations = [
        ("a", "b", "CALLS_ACTIVITY"),
        ("b", "c", "RUNS_DATA_TRANSFORM"),
        ("c", "a", "EVALUATES_WHEN"),
        ("b", "d", "USES_DATA_PAGE"),
    ]
    package = _project_package(project)
    package["entities"] = entities
    package["scope"].update({"entity_count": 4})
    package["status"].update({"entities": 4})
    package["relationships"] = [
        {
            "id": f"{project}::relationship:{index}",
            "source_entity_id": f"{project}::pega:{source}",
            "target_entity_id": f"{project}::pega:{target}",
            "relationship_type": relationship_type,
            "document_id": "",
            "properties": {"id": f"{project}::relationship:{index}"},
        }
        for index, (source, target, relationship_type) in enumerate(relations)
    ]
    selected = slice_project_package(
        package,
        seed_entity_id=f"{project}::pega:a",
        depth=1,
        relationship_types=["CALLS_ACTIVITY", "RUNS_DATA_TRANSFORM"],
        expand_entity_ids=[f"{project}::pega:b"],
        max_documents=10,
    )

    assert {entity["name"] for entity in selected["entities"]} == {"A", "B", "C"}
    assert {edge["relationship_type"] for edge in selected["relationships"]} == {
        "CALLS_ACTIVITY", "RUNS_DATA_TRANSFORM", "EVALUATES_WHEN"
    }
    assert selected["scope"]["complete_for_requested_scope"] is True
    assert selected["snapshot_key"] == digest(
        stable_json(
            {
                key: value
                for key, value in selected.items()
                if key not in {"requests", "snapshot_key"}
            }
        )
    )
