"""Automatic PEGA evidence freshness checks and reusable scoped captures."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codewiki.src.be.sources.evidence import digest, stable_json
from codewiki.src.be.sources.pega_mcp import (
    PegaGraphProvider,
    PegaMCPClient,
    save_evidence_package,
)


def scope_request(
    *,
    release: str = "lead",
    seed_id: str | None = None,
    seed_name: str | None = None,
    rule_type: str | None = None,
    class_name: str | None = None,
    ruleset: str | None = None,
    depth: int = 1,
    relationship_types: list[str] | None = None,
    relation_kinds: list[str] | None = None,
    expand_entity_ids: list[str] | None = None,
    max_documents: int = 40,
) -> dict[str, Any]:
    if not seed_id and not seed_name:
        return {"mode": "project", "release": release}
    return {
        "mode": "focused",
        "release": release,
        "seed_id": seed_id,
        "seed_name": seed_name.strip() if seed_name else None,
        "rule_type": rule_type,
        "class_name": class_name,
        "ruleset": ruleset,
        "depth": depth,
        "relationship_types": sorted(set(relationship_types or [])),
        "relation_kinds": sorted(set(relation_kinds or [])),
        "expand_entity_ids": sorted(set(expand_entity_ids or [])),
        "max_documents": max_documents,
    }


def package_projection_tokens(package: dict[str, Any]) -> set[str]:
    return {
        str(properties["projection_token"])
        for properties in (
            *[
                entity.get("properties") or {}
                for entity in package.get("entities") or []
            ],
            *[
                relationship.get("properties") or {}
                for relationship in package.get("relationships") or []
            ],
        )
        if properties.get("projection_token")
    }


def package_projection_revision(package: dict[str, Any]) -> str | None:
    status = package.get("status") or {}
    revision = status.get("project_projection_revision") or status.get(
        "projection_manifest_hash"
    )
    if revision:
        return str(revision)
    tokens = package_projection_tokens(package)
    if not tokens:
        return None
    return next(iter(tokens)) if len(tokens) == 1 else digest(stable_json(sorted(tokens)))


def stamp_project_revision(package: dict[str, Any], revision: str) -> None:
    """Bind a captured package to the live project revision used to verify it."""
    package["status"] = dict(package.get("status") or {})
    package["status"]["project_projection_revision"] = revision
    package["snapshot_key"] = digest(
        stable_json(
            {
                key: value
                for key, value in package.items()
                if key not in {"requests", "snapshot_key"}
            }
        )
    )


class PegaEvidenceStore:
    """Persist packages and source links independently from generated wiki runs."""

    def __init__(self, root: Path, project_id: str, release: str = "lead") -> None:
        self.project_root = root.expanduser().resolve() / digest(project_id)[:24]
        self.project_id = project_id
        self.release = release
        self.project_root.mkdir(parents=True, exist_ok=True)

    def request_key(self, request: dict[str, Any]) -> str:
        return digest(stable_json(request))[:24]

    def scope_dir(self, request: dict[str, Any]) -> Path:
        return self.project_root / "scopes" / self.request_key(request)

    @property
    def project_request(self) -> dict[str, Any]:
        return {"mode": "project", "release": self.release}

    @property
    def project_package(self) -> Path:
        return self.scope_dir(self.project_request) / "evidence-package.json"

    def package_path(self, request: dict[str, Any]) -> Path:
        return self.scope_dir(request) / "evidence-package.json"

    def evidence_dir(self, request: dict[str, Any]) -> Path:
        return self.scope_dir(request) / "evidence"

    def save(self, request: dict[str, Any], package: dict[str, Any]) -> Path:
        directory = self.scope_dir(request)
        directory.mkdir(parents=True, exist_ok=True)
        package_path = directory / "evidence-package.json"
        save_evidence_package(package, package_path)
        metadata_path = directory / "cache-record.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "project_id": self.project_id,
                    "scope_request": request,
                    "snapshot_key": package["snapshot_key"],
                    "projection_tokens": sorted(package_projection_tokens(package)),
                    "captured_at": datetime.now(UTC).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return package_path

    def latest_run(self, request: dict[str, Any]) -> Path | None:
        path = self.scope_dir(request) / "latest-run.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            run = Path(record["run_dir"]).expanduser().resolve()
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return None
        required = (
            run / "evidence-package.json",
            run / "evidence-manifest.json",
            run / "plan.json",
            run / "docs" / "module_tree.json",
        )
        return run if all(item.is_file() for item in required) else None

    def record_run(self, request: dict[str, Any], run_dir: Path) -> None:
        directory = self.scope_dir(request)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "latest-run.json").write_text(
            json.dumps({"run_dir": str(run_dir.resolve())}, indent=2) + "\n",
            encoding="utf-8",
        )


def find_latest_pega_run(
    *,
    cache_root: Path,
    search_roots: list[Path],
    project_id: str | None = None,
) -> Path | None:
    """Find the newest complete PEGA wiki from cache pointers or legacy run roots."""
    candidates: dict[Path, int] = {}
    cache_root = cache_root.expanduser().resolve()
    pointer_roots = (
        [cache_root / digest(project_id)[:24]]
        if project_id
        else [path for path in cache_root.glob("*") if path.is_dir()]
    )

    def consider(run_dir: Path, timestamp_ns: int) -> None:
        run_dir = run_dir.expanduser().resolve()
        required = (
            run_dir / "evidence-package.json",
            run_dir / "evidence-manifest.json",
            run_dir / "plan.json",
            run_dir / "docs" / "module_tree.json",
        )
        if not all(path.is_file() for path in required):
            return
        try:
            manifest = json.loads(required[0].read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            return
        if not isinstance(manifest, dict) or manifest.get("source_kind") != "pega":
            return
        if project_id and manifest.get("project_id") != project_id:
            return
        if run_dir not in candidates or timestamp_ns > candidates[run_dir]:
            candidates[run_dir] = timestamp_ns

    for project_root in pointer_roots:
        for pointer in (project_root / "scopes").glob("*/latest-run.json"):
            try:
                record = json.loads(pointer.read_text(encoding="utf-8"))
                run_dir = Path(record["run_dir"])
                timestamp_ns = pointer.stat().st_mtime_ns
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                continue
            consider(run_dir, timestamp_ns)

    for search_root in search_roots:
        root = search_root.expanduser().resolve()
        if not root.is_dir():
            continue
        for manifest_path in root.rglob("evidence-manifest.json"):
            if any(part.startswith(".") for part in manifest_path.relative_to(root).parts):
                continue
            try:
                timestamp_ns = manifest_path.stat().st_mtime_ns
            except OSError:
                continue
            consider(manifest_path.parent, timestamp_ns)

    return max(candidates, key=lambda path: (candidates[path], str(path))) if candidates else None


def _status_matches(package: dict[str, Any], live_status: dict[str, Any]) -> bool:
    cached = package.get("status") or {}
    return all(
        cached.get(key) == live_status.get(key)
        for key in (
            "project_id", "release", "slug", "content_hash",
            "entities", "documents", "relationships",
        )
    )


def _cached_revision_matches(
    package: dict[str, Any], live_revision: str | None, live_status: dict[str, Any]
) -> bool:
    if not live_revision:
        return False
    cached_revision = package_projection_revision(package)
    return (
        cached_revision == live_revision
        and _status_matches(package, live_status)
    )


def _resolve_cached_seed(
    package: dict[str, Any], request: dict[str, Any]
) -> str:
    seed_id = request.get("seed_id")
    if seed_id:
        return str(seed_id)
    seed_name = str(request.get("seed_name") or "")
    exact = [
        entity
        for entity in package.get("entities") or []
        if str(entity.get("name") or "").casefold() == seed_name.casefold()
        and (not request.get("rule_type") or entity.get("rule_type") == request["rule_type"])
        and (not request.get("class_name") or entity.get("class_name") == request["class_name"])
        and (not request.get("ruleset") or entity.get("ruleset") == request["ruleset"])
    ]
    if len(exact) != 1:
        cards = [
            {key: entity.get(key) for key in ("id", "name", "entity_type", "rule_type", "class_name", "ruleset")}
            for entity in exact
        ]
        raise ValueError(
            f"Cached project seed resolution returned {len(exact)} exact matches for "
            f"{seed_name!r}; candidates: {cards[:10]}"
        )
    return str(exact[0]["id"])


def slice_project_package(
    package: dict[str, Any],
    *,
    seed_entity_id: str,
    depth: int,
    relationship_types: list[str] | None,
    relation_kinds: list[str] | None = None,
    expand_entity_ids: list[str] | None,
    max_documents: int,
) -> dict[str, Any]:
    """Derive the same bounded selection from a complete cached project graph."""
    if package.get("scope", {}).get("mode") != "project":
        raise ValueError("Only a complete project package can be used to derive a focused slice")
    entities = {row["id"]: row for row in package.get("entities") or []}
    if seed_entity_id not in entities:
        raise KeyError(f"Seed entity {seed_entity_id} is absent from the cached project package")
    edges = package.get("relationships") or []
    selected_types = set(relationship_types or [])
    selected_kinds = set(relation_kinds or [])

    def neighbors(entity_id: str) -> set[str]:
        result: set[str] = set()
        for edge in edges:
            if entity_id not in (edge["source_entity_id"], edge["target_entity_id"]):
                continue
            relation_kind = (edge.get("properties") or {}).get("relation_kind")
            if selected_types and edge["relationship_type"] not in selected_types:
                continue
            if selected_kinds and (edge.get("properties") or {}).get("relation_kind") not in selected_kinds:
                continue
            if edge["relationship_type"] in {"DESCRIBES", "HAS_CHUNK"}:
                continue
            neighbor = (
                edge["target_entity_id"]
                if edge["source_entity_id"] == entity_id
                else edge["source_entity_id"]
            )
            if not neighbor.startswith((f"{package['project_id']}::document:", f"{package['project_id']}::chunk:")):
                result.add(neighbor)
        return result

    scope_ids = {seed_entity_id}
    frontier = {seed_entity_id}
    for _ in range(depth):
        next_frontier = {
            neighbor
            for entity_id in frontier
            for neighbor in neighbors(entity_id)
            if neighbor not in scope_ids
        }
        scope_ids.update(next_frontier)
        frontier = next_frontier
        if not frontier:
            break

    expansions = sorted(set(expand_entity_ids or []))
    if not set(expansions).issubset(scope_ids):
        raise ValueError("Expanded entity IDs must first be selected by the seed scope")
    for entity_id in expansions:
        if entity_id not in entities:
            raise KeyError(f"Expanded entity {entity_id} is absent from the cached project package")
        scope_ids.update(neighbors(entity_id))

    selected_entities = [entities[entity_id] for entity_id in sorted(scope_ids)]
    selected_relationships = []
    for edge in edges:
        if edge["source_entity_id"] not in scope_ids or edge["target_entity_id"] not in scope_ids:
            continue
        relation_kind = (edge.get("properties") or {}).get("relation_kind")
        if selected_types and edge["relationship_type"] not in selected_types:
            continue
        if selected_kinds and relation_kind not in selected_kinds:
            continue
        selected_relationships.append(edge)
    document_ids = sorted({
        document_id
        for entity in selected_entities
        for document_id in entity.get("document_ids") or []
    })
    if len(document_ids) > max_documents:
        raise ValueError(
            f"Slice selected {len(document_ids)} documents, over max_documents={max_documents}; "
            "refine the relationship scope"
        )
    documents = package.get("documents") or {}
    missing_documents = set(document_ids) - set(documents)
    if missing_documents:
        raise ValueError(f"Cached project package is missing documents: {sorted(missing_documents)}")
    selected_documents = {document_id: documents[document_id] for document_id in document_ids}
    unresolved = [
        {
            "relationship_id": edge["id"],
            "target_entity_id": edge["target_entity_id"],
            "resolution_outcome": (edge.get("properties") or {}).get("resolution_outcome"),
            "resolution_reason": (edge.get("properties") or {}).get("resolution_reason"),
        }
        for edge in selected_relationships
        if (edge.get("properties") or {}).get("resolution_outcome") in {"MISSING_EXPORT", "AMBIGUOUS_REFERENCE"}
    ]
    result = {
        "source_kind": "pega",
        "project_id": package["project_id"],
        "source_contract": package.get("source_contract"),
        "scope": {
            "mode": "focused",
            "seed_entity_ids": [seed_entity_id],
            "depth": depth,
            "relationship_types": sorted(selected_types),
            "relation_kinds": sorted(selected_kinds),
            "release": (package.get("scope") or {}).get("release"),
            "release_slug": (package.get("scope") or {}).get("release_slug"),
            "discovery": "cached_project_graph_bfs",
            "entity_count": len(scope_ids),
            "document_count": len(selected_documents),
            "complete_for_requested_scope": True,
        },
        "status": package["status"],
        "schema": package["schema"],
        "entities": selected_entities,
        "relationships": selected_relationships,
        "documents": selected_documents,
        "unresolved_references": unresolved,
        "requests": [],
    }
    if expansions:
        result["scope"]["expanded_entity_ids"] = expansions
        result["scope"]["expansion_depth"] = 1
    result["snapshot_key"] = digest(
        stable_json({key: value for key, value in result.items() if key != "requests"})
    )
    return result


async def prepare_pega_evidence(
    *,
    project: str,
    release: str = "lead",
    client: PegaMCPClient,
    source_root: Path,
    store: PegaEvidenceStore,
    request: dict[str, Any],
) -> tuple[PegaGraphProvider, dict[str, Any], dict[str, Any]]:
    """Check freshness, then reuse, locally derive, or capture the requested evidence."""
    scope_dir = store.scope_dir(request)
    scope_dir.mkdir(parents=True, exist_ok=True)
    provider = PegaGraphProvider(
        client,
        project=project,
        release=release,
        cache_dir=store.evidence_dir(request),
        source_root=source_root,
    )
    await provider.initialize(lightweight=True)
    live_tokens = await provider.project_projection_tokens()
    provider._require_ready_status()
    if not provider.has_project_manifest and len(live_tokens) > 1:
        raise RuntimeError(
            "PEGA project has mixed row projection tokens and no project manifest; "
            "wait for indexing to finish and retry"
        )
    live_revision = provider.project_revision
    if not live_revision:
        raise RuntimeError(
            "Cannot determine the PEGA project revision: the graph has no project manifest "
            "or projected entity tokens"
        )
    package_path = store.package_path(request)

    if package_path.is_file() and live_revision:
        raw_package = json.loads(package_path.read_text(encoding="utf-8"))
        if _cached_revision_matches(raw_package, live_revision, provider.status or {}):
            package = provider.load_snapshot(package_path)
            package["requests"] = []
            return provider, package, {
                "decision": "reused_scope_package",
                "scope_key": store.request_key(request),
                "project_revision": live_revision,
            }

    if request.get("mode") == "focused" and live_revision:
        full_path = store.project_package
        if full_path.is_file():
            full_provider = PegaGraphProvider(
                None,
                project=project,
                release=release,
                cache_dir=store.evidence_dir(store.project_request),
                source_root=source_root,
            )
            full_raw = json.loads(full_path.read_text(encoding="utf-8"))
            if _cached_revision_matches(full_raw, live_revision, provider.status or {}):
                full_package = full_provider.load_snapshot(full_path)
                seed_id = _resolve_cached_seed(full_package, request)
                package = slice_project_package(
                    full_package,
                    seed_entity_id=seed_id,
                    depth=int(request["depth"]),
                    relationship_types=request.get("relationship_types") or None,
                    relation_kinds=request.get("relation_kinds") or None,
                    expand_entity_ids=request.get("expand_entity_ids") or None,
                    max_documents=int(request["max_documents"]),
                )
                full_provider.cache.publish_to(
                    provider.cache.root,
                    document_ids=set(package["documents"]),
                )
                save_evidence_package(package, package_path)
                package = provider.load_snapshot(package_path)
                return provider, package, {
                    "decision": "derived_slice_from_cached_project",
                    "scope_key": store.request_key(request),
                    "project_revision": live_revision,
                    "parent_snapshot_key": full_package["snapshot_key"],
                }

    await provider.load_schema()
    if request.get("mode") == "focused":
        if request.get("seed_id") or request.get("seed_name"):
            from codewiki.cli.commands.pega import _resolve_seed

            seed_id = await _resolve_seed(
                provider,
                request.get("seed_id"),
                request.get("seed_name"),
                request.get("rule_type"),
                request.get("class_name"),
                request.get("ruleset"),
            )
        else:
            raise ValueError("Focused PEGA scope requires a seed ID or exact seed name")
        package = await provider.snapshot_slice(
            seed_entity_id=seed_id,
            depth=int(request["depth"]),
            relationship_types=request.get("relationship_types") or None,
            relation_kinds=request.get("relation_kinds") or None,
            expand_entity_ids=request.get("expand_entity_ids") or [],
            max_documents=int(request["max_documents"]),
        )
        decision = "captured_focused_scope"
    else:
        package = await provider.snapshot_project()
        decision = "captured_complete_project"

    # Verify that the live index did not move while graph and documents were
    # being gathered. The KBProject manifest and row tokens already emitted by
    # the PEGA Agent provide this check; no agent output or MCP schema changes.
    await provider._verify_status_unchanged()
    await provider.project_projection_tokens()
    provider._require_ready_status()
    if provider.project_revision != live_revision:
        raise RuntimeError(
            "PEGA project revision changed during evidence capture; retry generation"
        )
    stamp_project_revision(package, live_revision)
    store.save(request, package)
    return provider, package, {
        "decision": decision,
        "scope_key": store.request_key(request),
        "project_revision": live_revision,
    }
