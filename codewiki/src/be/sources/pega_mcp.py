"""Project-scoped Pega MCP transport and deterministic evidence acquisition."""

from __future__ import annotations

import json
import inspect
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Mapping, Protocol

from codewiki.src.be.dependency_analyzer.models.core import Node

from .evidence import EvidenceCache, PegaDocument, PegaEntity, PegaRelationship, digest, stable_json


# CodeWiki's untyped ``depends_on`` is only a behavioral navigation view.
# Ontology, ownership, class, and property edges remain in the typed package.
COMPATIBILITY_DEPENDENCY_TYPES = frozenset(
    {
        "CALLS",
        "READS",
        "WRITES",
        "APPLIES_DATA_TRANSFORM",
        "CALLS_ACTIVITY",
        "CALLS_UTILITY_FUNCTION",
        "DATA_PAGE_LOADS_WITH_ACTIVITY",
        "DATA_PAGE_SOURCES_FROM_REPORT_DEFINITION",
        "EVALUATES_DECISION_TABLE",
        "EVALUATES_WHEN",
        "ROUTES_TO_DATA_PAGE",
        "ROUTES_TO_UI_VIEW",
        "RUNS_DATA_TRANSFORM",
        "SCHEDULES_ACTIVITY",
        "SERVICE_INVOKES_ACTIVITY",
        "USES_DATA_PAGE",
    }
)


class ToolTransport(Protocol):
    async def call(self, name: str, arguments: dict[str, Any]) -> Any: ...


class PegaMCPClient:
    """Own one stdio MCP session for a documentation run."""

    def __init__(
        self,
        command: str,
        args: list[str],
        *,
        cwd: str,
        env: Mapping[str, str] | None = None,
        exclude_env: set[str] | None = None,
    ) -> None:
        self.command = command
        self.args = args
        self.cwd = cwd
        self.env = dict(env or {})
        self.exclude_env = set(exclude_env or ())
        self._stack: AsyncExitStack | None = None
        self._session: Any = None

    async def __aenter__(self) -> PegaMCPClient:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        stack = AsyncExitStack()
        try:
            params = StdioServerParameters(
                command=self.command,
                args=self.args,
                cwd=self.cwd,
                env={
                    key: value
                    for key, value in {**os.environ, **self.env}.items()
                    if key not in self.exclude_env
                },
            )
            read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            self._stack = stack
            self._session = session
            return self
        except BaseException:
            await stack.aclose()
            raise

    async def __aexit__(self, *_exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._session is None:
            raise RuntimeError("Pega MCP session is not open")
        result = await self._session.call_tool(name, arguments)
        if result.isError:
            messages = [str(getattr(item, "text", "")) for item in result.content]
            raise RuntimeError(f"Pega MCP {name} failed: {' '.join(messages)}")
        structured = getattr(result, "structuredContent", None)
        if structured is None:
            structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            if set(structured) == {"result"}:
                return structured["result"]
            return structured
        for item in result.content:
            raw_text = getattr(item, "text", None)
            if not isinstance(raw_text, str):
                continue
            decoded = json.loads(raw_text)
            if decoded is None or isinstance(decoded, (dict, list)):
                return decoded
        raise ValueError(f"Pega MCP {name} returned no JSON object")


class PegaGraphProvider:
    """Read a selected Pega project through its public MCP contract only."""

    def __init__(
        self,
        transport: ToolTransport | None,
        *,
        project: str,
        cache_dir: Path,
        source_root: Path | None = None,
        max_pages: int = 30,
    ) -> None:
        if not project.strip():
            raise ValueError("A Pega project ID is required")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self.transport = transport
        self.project = project
        self.cache = EvidenceCache(cache_dir, source_root=source_root)
        self.max_pages = max_pages
        self.status: dict[str, Any] | None = None
        self.schema: dict[str, Any] | None = None
        self.project_revision: str | None = None
        self.project_manifest_hash: str | None = None
        self.project_projection_status: str | None = None
        self.has_project_manifest = False
        self._selected_document_ids: set[str] = set()
        self._entities: dict[str, PegaEntity] = {}
        self._relationships: dict[str, PegaRelationship] = {}
        self._requests: list[dict[str, Any]] = []
        self._evidence_reads: list[dict[str, Any]] = []
        self._specialist_events: list[dict[str, Any]] = []

    async def _call(self, name: str, **arguments: Any) -> dict[str, Any] | None:
        if self.transport is None:
            raise RuntimeError("This Pega provider is a frozen snapshot with no live MCP transport")
        if name != "list_projects":
            arguments = {"project": self.project, **arguments}
        result = await self.transport.call(name, arguments)
        self._requests.append({"tool": name, "arguments": arguments})
        if result is not None and not isinstance(result, dict):
            raise TypeError(f"Pega MCP {name} returned {type(result).__name__}, expected object")
        return result

    async def initialize(self, *, lightweight: bool = False) -> None:
        projects = await self._call("list_projects") or {}
        known = {
            str(item.get("project_id"))
            for item in projects.get("projects", [])
            if isinstance(item, dict)
        }
        if self.project not in known:
            raise ValueError(f"Project {self.project!r} is not in list_projects: {sorted(known)}")
        self.status = await self._call("kb_status") or {}
        if self.status.get("project_id") != self.project:
            raise ValueError("kb_status returned a different project")
        self.schema = None if lightweight else await self._call("describe_graph") or {}

    async def load_schema(self) -> dict[str, Any]:
        if self.status is None:
            raise RuntimeError("Call initialize() before loading the Pega graph schema")
        self.schema = await self._call("describe_graph") or {}
        return self.schema

    async def project_projection_tokens(self) -> list[str]:
        """Read the existing project manifest, falling back to projected row tokens.

        The manifest is queried directly through the existing PEGA MCP Cypher
        reader. This keeps freshness checks independent of changes to the
        PEGA Agent tool contract.
        """
        if self.status is None:
            raise RuntimeError("Call initialize() before checking the Pega project revision")
        manifest = await self._call(
            "read_cypher",
            statement=(
                "OPTIONAL MATCH (manifest:KBProject {project_id: $project_id}) "
                "RETURN collect(manifest.projection_manifest_hash)[0] AS projection_manifest_hash, "
                "collect(manifest.status)[0] AS projection_status"
            ),
            parameters={},
        ) or {}
        manifest_rows = manifest.get("rows")
        if not isinstance(manifest_rows, list) or len(manifest_rows) != 1:
            raise ValueError("Project manifest query returned an invalid result")
        manifest_hash = manifest_rows[0].get("projection_manifest_hash")
        self.project_manifest_hash = str(manifest_hash) if manifest_hash else None
        self.project_projection_status = manifest_rows[0].get("projection_status")
        self.has_project_manifest = bool(self.project_manifest_hash)
        if self.project_manifest_hash:
            self.project_revision = self.project_manifest_hash
            return [self.project_manifest_hash]

        result = await self._call(
            "read_cypher",
            statement=(
                "MATCH (entity:PegaEntity {project_id: $project_id}) "
                "RETURN collect(DISTINCT entity.projection_token) AS projection_tokens"
            ),
            parameters={},
        ) or {}
        rows = result.get("rows")
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("Project projection revision query returned an invalid result")
        values = rows[0].get("projection_tokens")
        if not isinstance(values, list):
            raise ValueError("Project projection revision query returned no token list")
        tokens = sorted({str(value) for value in values if value})
        self.project_revision = digest(stable_json(tokens)) if tokens else None
        return tokens

    def _require_initialized(self) -> None:
        if self.status is None or self.schema is None:
            raise RuntimeError("Call initialize() before Pega retrieval")

    def _require_ready_status(self) -> None:
        assert self.status is not None
        projection_status = (
            self.status.get("projection_status") or self.project_projection_status
        )
        if projection_status and projection_status != "ready":
            raise RuntimeError(
                f"PEGA project projection is {projection_status!r}; wait until indexing is ready"
            )

    async def _verify_status_unchanged(self) -> dict[str, Any]:
        assert self.status is not None
        latest = await self._call("kb_status") or {}
        for key in (
            "entities", "documents", "relationships",
            "projection_manifest_hash", "projection_status",
        ):
            if latest.get(key) != self.status.get(key):
                raise RuntimeError(f"PEGA project {key} changed during evidence capture")
        self.status = latest
        self._require_ready_status()
        return latest

    def _remember_candidates(self, payload: dict[str, Any]) -> None:
        for candidate in payload.get("candidate_documents") or []:
            if isinstance(candidate, dict) and candidate.get("document_id"):
                self._selected_document_ids.add(str(candidate["document_id"]))
        for document in payload.get("source_documents") or []:
            if isinstance(document, dict) and document.get("document_id"):
                self._selected_document_ids.add(str(document["document_id"]))

    async def _pages(self, name: str, result_key: str, **arguments: Any) -> list[dict[str, Any]]:
        self._require_initialized()
        offset = 0
        collected: list[dict[str, Any]] = []
        for _page in range(self.max_pages):
            payload = await self._call(name, offset=offset, **arguments) or {}
            self._remember_candidates(payload)
            items = payload.get(result_key)
            if not isinstance(items, list):
                raise ValueError(f"Pega MCP {name} omitted {result_key}")
            collected.extend(item for item in items if isinstance(item, dict))
            if not payload.get("has_more"):
                return collected
            next_offset = payload.get("next_offset")
            if not isinstance(next_offset, int) or next_offset <= offset:
                raise ValueError(f"Pega MCP {name} returned invalid next_offset")
            offset = next_offset
        raise RuntimeError(f"Pega MCP {name} exceeded {self.max_pages} pages; scope is incomplete")

    async def search_entities(
        self,
        query: str = "",
        *,
        rule_types: list[str] | None = None,
        class_names: list[str] | None = None,
        rulesets: list[str] | None = None,
        entity_types: list[str] | None = None,
        include_external: bool = False,
    ) -> list[PegaEntity]:
        rows = await self._pages(
            "search_entities",
            "results",
            query=query,
            rule_types=rule_types,
            class_names=class_names,
            rulesets=rulesets,
            entity_types=entity_types,
            include_external=include_external,
            limit=500,
        )
        found: list[PegaEntity] = []
        for row in rows:
            raw = row.get("entity")
            if not isinstance(raw, dict):
                continue
            source_documents = row.get("source_documents") or []
            document_ids = [
                str(item["document_id"])
                for item in source_documents
                if isinstance(item, dict) and item.get("document_id")
            ]
            self._selected_document_ids.update(document_ids)
            entity = PegaEntity.from_result(raw, document_ids)
            self._entities[entity.id] = entity
            found.append(entity)
        return found

    async def get_entity(self, entity_id: str) -> PegaEntity:
        self._require_initialized()
        self._check_id(entity_id, "entity")
        payload = await self._call("get_entity", entity_id=entity_id)
        if not payload or not isinstance(payload.get("entity"), dict):
            raise KeyError(f"Pega entity {entity_id} was not found")
        self._remember_candidates(payload)
        docs = [
            str(item["document_id"])
            for item in payload.get("source_documents") or []
            if isinstance(item, dict) and item.get("document_id")
        ]
        entity = PegaEntity.from_result(payload["entity"], docs)
        if entity.id != entity_id:
            raise ValueError("get_entity returned a different graph ID")
        self._entities[entity.id] = entity
        for direction, field in (("outgoing", "target_id"), ("incoming", "source_id")):
            for item in payload.get(direction) or []:
                if not isinstance(item, dict) or not item.get(field) or not item.get("type"):
                    continue
                props = item.get("properties") or {}
                if not isinstance(props, dict) or not props.get("id"):
                    continue
                raw = {
                    "source_entity_id": entity.id if direction == "outgoing" else item[field],
                    "target_entity_id": item[field] if direction == "outgoing" else entity.id,
                    "relationship_type": item["type"],
                    "properties": props,
                }
                relation = PegaRelationship.from_result(raw)
                self._relationships[relation.id] = relation
                if relation.document_id:
                    self._selected_document_ids.add(relation.document_id)
        return entity

    async def traverse(
        self,
        entity_ids: list[str],
        *,
        depth: int = 1,
        direction: str = "both",
        relationship_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        for entity_id in entity_ids:
            self._check_id(entity_id, "entity")
        if not 1 <= depth <= 8 or not 1 <= len(entity_ids) <= 50:
            raise ValueError("traverse requires 1–50 seeds and depth 1–8")
        paths = await self._pages(
            "traverse_graph",
            "paths",
            entity_ids=entity_ids,
            depth=depth,
            direction=direction,
            relationship_types=relationship_types,
            limit=500,
        )
        for path in paths:
            for raw in path.get("nodes") or []:
                if isinstance(raw, dict) and raw.get("id"):
                    entity = PegaEntity.from_result(raw)
                    prior = self._entities.get(entity.id)
                    if prior and prior.document_ids:
                        entity = PegaEntity.from_result(raw, list(prior.document_ids))
                    self._entities[entity.id] = entity
            for document_id in path.get("path_document_ids") or []:
                if isinstance(document_id, str) and document_id:
                    self._selected_document_ids.add(document_id)
        return paths

    async def get_document(self, document_id: str) -> PegaDocument:
        self._require_initialized()
        self._check_id(document_id, "document")
        if document_id not in self._selected_document_ids:
            raise ValueError("Document must first be selected by a graph result")
        cached = self.cache.get_document(document_id)
        if cached is not None:
            return cached
        payload = await self._call("get_document", document_id=document_id)
        if not payload:
            raise KeyError(f"Official document {document_id} was not found")
        document = PegaDocument.from_result(document_id, payload)
        self.cache.put_document(document)
        return document

    async def snapshot_slice(
        self,
        *,
        seed_entity_id: str,
        depth: int = 1,
        relationship_types: list[str] | None = None,
        relation_kinds: list[str] | None = None,
        expand_entity_ids: list[str] | None = None,
        max_documents: int = 40,
    ) -> dict[str, Any]:
        """Capture a bounded graph slice and official documents into the cache."""
        self._require_initialized()
        self._require_ready_status()
        self._check_id(seed_entity_id, "entity")
        if not 1 <= depth <= 8:
            raise ValueError("depth must be between 1 and 8")
        selected_types = set(relationship_types or [])
        schema_types = {
            str(shape.get("relationship_type"))
            for shape in (self.schema or {}).get("relationship_shapes", [])
            if isinstance(shape, dict)
        }
        schema_types.update(str(item) for item in (self.schema or {}).get("edge_types", []))
        unknown_types = selected_types - schema_types
        if unknown_types:
            raise ValueError(f"Relationship types absent from live schema: {sorted(unknown_types)}")
        # Direct entity inspection retains canonical edge direction and properties.
        # The MCP traversal's path ordering can tie across pages, so it is not
        # used as a completeness proof for this bounded snapshot.
        scope_ids = {seed_entity_id}
        frontier = {seed_entity_id}
        for _level in range(depth):
            next_frontier: set[str] = set()
            for entity_id in sorted(frontier):
                await self.get_entity(entity_id)
                for relation in self._relationships.values():
                    if entity_id not in (relation.source_entity_id, relation.target_entity_id):
                        continue
                    if selected_types and relation.relationship_type not in selected_types:
                        continue
                    if relation_kinds and relation.properties.get("relation_kind") not in set(relation_kinds):
                        continue
                    if relation.relationship_type in {"DESCRIBES", "HAS_CHUNK"}:
                        continue
                    neighbor = (
                        relation.target_entity_id
                        if relation.source_entity_id == entity_id
                        else relation.source_entity_id
                    )
                    if neighbor.startswith((f"{self.project}::document:", f"{self.project}::chunk:")):
                        continue
                    if neighbor not in scope_ids:
                        next_frontier.add(neighbor)
            scope_ids.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break
        for entity_id in sorted(scope_ids):
            if entity_id not in self._entities or not self._entities[entity_id].document_ids:
                await self.get_entity(entity_id)
        expansions = sorted(set(expand_entity_ids or []))
        if not set(expansions).issubset(scope_ids):
            raise ValueError("Expanded entity IDs must first be selected by the seed scope")
        for entity_id in expansions:
            self._check_id(entity_id, "expanded entity")
            await self.get_entity(entity_id)
            for relation in self._relationships.values():
                if entity_id not in (relation.source_entity_id, relation.target_entity_id):
                    continue
                if selected_types and relation.relationship_type not in selected_types:
                    continue
                if relation_kinds and relation.properties.get("relation_kind") not in set(relation_kinds):
                    continue
                if relation.relationship_type in {"DESCRIBES", "HAS_CHUNK"}:
                    continue
                neighbor = (
                    relation.target_entity_id
                    if relation.source_entity_id == entity_id
                    else relation.source_entity_id
                )
                if not neighbor.startswith((f"{self.project}::document:", f"{self.project}::chunk:")):
                    scope_ids.add(neighbor)
        for entity_id in sorted(scope_ids):
            if entity_id not in self._entities or not self._entities[entity_id].document_ids:
                await self.get_entity(entity_id)
        scoped_relationships = {
            relation.id: relation
            for relation in self._relationships.values()
            if relation.source_entity_id in scope_ids and relation.target_entity_id in scope_ids
            and (not selected_types or relation.relationship_type in selected_types)
            and (not relation_kinds or relation.properties.get("relation_kind") in set(relation_kinds))
        }
        projection_tokens = {
            str(properties["projection_token"])
            for properties in (
                *[self._entities[entity_id].properties for entity_id in scope_ids],
                *[relation.properties for relation in scoped_relationships.values()],
            )
            if properties.get("projection_token")
        }
        if len(projection_tokens) > 1 and not self.has_project_manifest:
            raise RuntimeError("Pega slice contains mixed projection tokens; capture a stable KB")
        document_ids = sorted(
            {
                document_id
                for entity_id in scope_ids
                for document_id in self._entities[entity_id].document_ids
            }
        )
        if len(document_ids) > max_documents:
            raise RuntimeError(
                f"Slice selected {len(document_ids)} documents, over max_documents={max_documents}; "
                "refine the relationship scope"
            )
        for document_id in document_ids:
            await self.get_document(document_id)
        await self._verify_status_unchanged()
        package = {
            "source_kind": "pega",
            "project_id": self.project,
            "scope": {
                "seed_entity_ids": [seed_entity_id],
                "depth": depth,
                "relationship_types": sorted(selected_types),
                "relation_kinds": sorted(set(relation_kinds or [])),
                "release": self.release_id,
                "release_slug": self.release_slug,
                "discovery": "directed_get_entity_bfs",
                "entity_count": len(scope_ids),
                "document_count": len(document_ids),
                "complete_for_requested_scope": True,
            },
            "status": self.status,
            "schema": self.schema,
            "entities": [self._entities[key].card() | {"properties": self._entities[key].properties} for key in sorted(scope_ids)],
            "relationships": [
                {
                    "id": relation.id,
                    "source_entity_id": relation.source_entity_id,
                    "target_entity_id": relation.target_entity_id,
                    "relationship_type": relation.relationship_type,
                    "document_id": relation.document_id,
                    "properties": relation.properties,
                }
                for relation in sorted(scoped_relationships.values(), key=lambda item: item.id)
            ],
            "documents": self.cache.manifest(),
            "unresolved_references": [
                {
                    "relationship_id": relation.id,
                    "target_entity_id": relation.target_entity_id,
                    "resolution_outcome": relation.properties.get("resolution_outcome"),
                    "resolution_reason": relation.properties.get("resolution_reason"),
                }
                for relation in sorted(scoped_relationships.values(), key=lambda item: item.id)
                if relation.properties.get("resolution_outcome") in {"MISSING_EXPORT", "AMBIGUOUS_REFERENCE"}
            ],
            "requests": self._requests,
        }
        if expansions:
            package["scope"]["expanded_entity_ids"] = expansions
            package["scope"]["expansion_depth"] = 1
        package["snapshot_key"] = digest(
            stable_json({key: value for key, value in package.items() if key != "requests"})
        )
        return package

    async def snapshot_project(self) -> dict[str, Any]:
        """Capture every project entity, domain edge, and linked official document.

        Search pagination establishes the entity/document inventory. A fixed,
        project-scoped Cypher query enumerates directed edges in stable ID order;
        this avoids depending on a guessed entry rule or graph connectivity.
        """
        self._require_initialized()
        assert self.status is not None
        self._require_ready_status()
        expected_entities = int(self.status.get("entities", -1))
        expected_documents = int(self.status.get("documents", -1))
        if expected_entities < 1 or expected_documents < 1:
            raise ValueError("The project KB has no complete entity/document inventory")

        entities = await self.search_entities("", include_external=True)
        entity_ids = {entity.id for entity in entities}
        if len(entities) != len(entity_ids) or len(entity_ids) != expected_entities:
            raise RuntimeError(
                f"Project entity inventory is incomplete: {len(entity_ids)} unique "
                f"of {expected_entities} reported by kb_status"
            )

        edge_pattern = (
            "MATCH (source:PegaEntity {project_id: $project_id})"
            "-[edge {project_id: $project_id}]->"
            "(target:PegaEntity {project_id: $project_id}) "
        )
        count_result = await self._call(
            "read_cypher",
            statement=edge_pattern + "RETURN count(edge) AS relationship_count",
            parameters={},
        ) or {}
        count_rows = count_result.get("rows")
        if not isinstance(count_rows, list) or len(count_rows) != 1:
            raise ValueError("Project relationship count query returned an invalid result")
        expected_edges = int(count_rows[0].get("relationship_count", -1))
        if expected_edges < 0:
            raise ValueError("Project relationship count is unavailable")

        page_size = 500
        relationships: dict[str, PegaRelationship] = {}
        for offset in range(0, expected_edges, page_size):
            result = await self._call(
                "read_cypher",
                statement=(
                    edge_pattern
                    + "RETURN source.id AS source_entity_id, "
                    "target.id AS target_entity_id, "
                    "type(edge) AS relationship_type, properties(edge) AS evidence "
                    "ORDER BY edge.id SKIP $offset LIMIT $limit"
                ),
                parameters={"offset": offset, "limit": page_size},
            ) or {}
            rows = result.get("rows")
            if not isinstance(rows, list) or len(rows) != min(page_size, expected_edges - offset):
                raise RuntimeError(f"Project relationship page at offset {offset} is incomplete")
            for row in rows:
                relation = PegaRelationship.from_result(row)
                if relation.id in relationships:
                    raise RuntimeError(f"Duplicate project relationship ID {relation.id}")
                if relation.source_entity_id not in entity_ids or relation.target_entity_id not in entity_ids:
                    raise RuntimeError(f"Project relationship {relation.id} has an unknown endpoint")
                relationships[relation.id] = relation
                if relation.document_id:
                    self._selected_document_ids.add(relation.document_id)
        if len(relationships) != expected_edges:
            raise RuntimeError("Project relationship inventory changed during pagination")
        self._relationships = relationships

        document_ids = sorted({document_id for entity in entities for document_id in entity.document_ids})
        if len(document_ids) != expected_documents:
            raise RuntimeError(
                f"Project document inventory is incomplete: {len(document_ids)} linked "
                f"of {expected_documents} reported by kb_status"
            )
        if any(relation.document_id and relation.document_id not in document_ids for relation in relationships.values()):
            raise RuntimeError("A project relationship references a document outside the inventory")
        for document_id in document_ids:
            await self.get_document(document_id)

        tokens = {
            str(properties["projection_token"])
            for properties in (
                *(entity.properties for entity in entities),
                *(relation.properties for relation in relationships.values()),
            )
            if properties.get("projection_token")
        }
        if len(tokens) > 1 and not self.has_project_manifest:
            raise RuntimeError("Project inventory contains mixed projection tokens; capture a stable KB")
        await self._verify_status_unchanged()

        package = {
            "source_kind": "pega",
            "project_id": self.project,
            "scope": {
                "mode": "project",
                "seed_entity_ids": [],
                "relationship_types": [],
                "discovery": "complete_project_inventory",
                "entity_count": len(entity_ids),
                "relationship_count": len(relationships),
                "document_count": len(document_ids),
                "complete_for_requested_scope": True,
            },
            "status": self.status,
            "schema": self.schema,
            "entities": [self._entities[key].card() | {"properties": self._entities[key].properties} for key in sorted(entity_ids)],
            "relationships": [
                {
                    "id": relation.id,
                    "source_entity_id": relation.source_entity_id,
                    "target_entity_id": relation.target_entity_id,
                    "relationship_type": relation.relationship_type,
                    "document_id": relation.document_id,
                    "properties": relation.properties,
                }
                for relation in sorted(relationships.values(), key=lambda item: item.id)
            ],
            "documents": self.cache.manifest(),
            "unresolved_references": [
                {
                    "relationship_id": relation.id,
                    "target_entity_id": relation.target_entity_id,
                    "resolution_outcome": relation.properties.get("resolution_outcome"),
                    "resolution_reason": relation.properties.get("resolution_reason"),
                }
                for relation in sorted(relationships.values(), key=lambda item: item.id)
                if relation.properties.get("resolution_outcome") in {"MISSING_EXPORT", "AMBIGUOUS_REFERENCE"}
            ],
            "requests": self._requests,
        }
        package["snapshot_key"] = digest(
            stable_json({key: value for key, value in package.items() if key != "requests"})
        )
        return package

    def _check_id(self, value: str, kind: str) -> None:
        if not isinstance(value, str) or not value.startswith(f"{self.project}::"):
            raise ValueError(f"{kind} ID does not belong to project {self.project!r}: {value!r}")

    def load_snapshot(
        self, path: Path, *, verify_document_content: bool = True
    ) -> dict[str, Any]:
        """Restore a captured package, optionally checking current source bytes."""
        package = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(package, dict) or package.get("source_kind") != "pega":
            raise ValueError("The evidence package is not a Pega snapshot")
        if package.get("project_id") != self.project:
            raise ValueError("The evidence package belongs to a different Pega project")
        expected_key = digest(
            stable_json(
                {key: value for key, value in package.items() if key not in {"requests", "snapshot_key"}}
            )
        )
        if package.get("snapshot_key") != expected_key:
            raise ValueError("The saved Pega evidence package has an invalid snapshot_key")
        scope = package.get("scope")
        if not isinstance(scope, dict) or not scope.get("complete_for_requested_scope"):
            raise ValueError("The saved Pega evidence package is partial")
        documents = package.get("documents")
        entities = package.get("entities")
        relationships = package.get("relationships")
        if not isinstance(documents, dict):
            raise ValueError("The saved Pega evidence package has no document manifest")
        if not isinstance(entities, list) or not isinstance(relationships, list):
            raise ValueError("The saved Pega evidence package has no graph inventory")
        if scope.get("entity_count") != len(entities) or scope.get("document_count") != len(documents):
            raise ValueError("The saved Pega evidence counts disagree with its inventory")
        if scope.get("mode") == "project":
            status = package.get("status") or {}
            if (
                scope.get("relationship_count") != len(relationships)
                or status.get("entities") != len(entities)
                or status.get("documents") != len(documents)
            ):
                raise ValueError("The saved project evidence does not cover its reported inventory")
        self._selected_document_ids.clear()
        self._entities.clear()
        self._relationships.clear()
        for document_id, manifest_entry in documents.items():
            self._check_id(document_id, "document")
            if not isinstance(manifest_entry, dict):
                raise ValueError(f"Invalid document manifest entry for {document_id}")
            self.cache.load_document(
                document_id, manifest_entry, verify_content=verify_document_content
            )
            self._selected_document_ids.add(document_id)
        for row in entities:
            if not isinstance(row, dict) or not isinstance(row.get("properties"), dict):
                raise ValueError("Invalid Pega entity in saved package")
            entity = PegaEntity.from_result(row["properties"], row.get("document_ids") or [])
            if entity.id != row.get("id"):
                raise ValueError("Pega entity card and graph ID disagree")
            self._check_id(entity.id, "entity")
            if not set(entity.document_ids).issubset(documents):
                raise ValueError(f"Pega entity {entity.id} refers to an uncached document")
            if entity.id in self._entities:
                raise ValueError(f"Duplicate Pega entity {entity.id}")
            self._entities[entity.id] = entity
        for row in relationships:
            if not isinstance(row, dict):
                raise ValueError("Invalid Pega relationship in saved package")
            relation = PegaRelationship.from_result(row)
            if relation.id != row.get("id"):
                raise ValueError("Pega relationship card and graph ID disagree")
            self._check_id(relation.id, "relationship")
            if relation.source_entity_id not in self._entities or relation.target_entity_id not in self._entities:
                raise ValueError(f"Pega relationship {relation.id} escapes the captured scope")
            if relation.document_id and relation.document_id not in documents:
                raise ValueError(f"Pega relationship {relation.id} refers to an uncached document")
            if relation.id in self._relationships:
                raise ValueError(f"Duplicate Pega relationship {relation.id}")
            self._relationships[relation.id] = relation
        self.status = package.get("status")
        self.schema = package.get("schema")
        if not isinstance(self.status, dict) or not isinstance(self.schema, dict):
            raise ValueError("The saved Pega evidence package has no status or graph schema")
        return package

    def bind_snapshot(self, package: dict[str, Any]) -> None:
        """Restrict all in-memory graph and document selection to one package."""
        if package.get("source_kind") != "pega" or package.get("project_id") != self.project:
            raise ValueError("Cannot bind provider to an incompatible Pega evidence package")
        documents = package.get("documents")
        entities = package.get("entities")
        relationships = package.get("relationships")
        if (
            not isinstance(documents, dict)
            or not isinstance(entities, list)
            or not isinstance(relationships, list)
        ):
            raise ValueError("Cannot bind provider to an incomplete Pega evidence package")

        selected_entities: dict[str, PegaEntity] = {}
        for row in entities:
            if not isinstance(row, dict) or not isinstance(row.get("properties"), dict):
                raise ValueError("Invalid Pega entity in evidence package")
            entity = PegaEntity.from_result(row["properties"], row.get("document_ids") or [])
            if entity.id != row.get("id") or not set(entity.document_ids).issubset(documents):
                raise ValueError(f"Pega entity {entity.id} is inconsistent with the evidence package")
            selected_entities[entity.id] = entity

        selected_relationships: dict[str, PegaRelationship] = {}
        for row in relationships:
            if not isinstance(row, dict):
                raise ValueError("Invalid Pega relationship in evidence package")
            relationship = PegaRelationship.from_result(row)
            if (
                relationship.id != row.get("id")
                or relationship.source_entity_id not in selected_entities
                or relationship.target_entity_id not in selected_entities
                or (relationship.document_id and relationship.document_id not in documents)
            ):
                raise ValueError(f"Pega relationship {relationship.id} escapes the evidence package")
            selected_relationships[relationship.id] = relationship

        self._entities = selected_entities
        self._relationships = selected_relationships
        self._selected_document_ids = set(documents)

    def codewiki_components(self, package: dict[str, Any]) -> dict[str, Node]:
        """Expose a directed dependency view while retaining typed edges in the package.

        Only documented rules become CodeWiki components. The original edge
        records remain authoritative for type, qualifiers, call sites, and
        provenance; ``depends_on`` is deliberately only a compatibility view.
        """
        if package.get("project_id") != self.project:
            raise ValueError("Evidence package belongs to a different Pega project")
        documents = package.get("documents") or {}
        eligible = {
            str(entity["id"]): entity
            for entity in package.get("entities") or []
            if entity.get("rule_type")
            and any(document_id in documents for document_id in entity.get("document_ids") or [])
        }
        dependencies: dict[str, set[str]] = {entity_id: set() for entity_id in eligible}
        for edge in package.get("relationships") or []:
            source_id = edge["source_entity_id"]
            target_id = edge["target_entity_id"]
            if (
                source_id in eligible
                and target_id in eligible
                and edge["relationship_type"] in COMPATIBILITY_DEPENDENCY_TYPES
            ):
                dependencies[source_id].add(target_id)

        components: dict[str, Node] = {}
        for entity_id, entity in sorted(eligible.items()):
            document_id = next(
                document_id for document_id in entity["document_ids"] if document_id in documents
            )
            document_path = self.cache.markdown_path(document_id)
            card = {
                key: entity.get(key)
                for key in (
                    "id", "name", "entity_type", "rule_type", "rule_category",
                    "class_name", "ruleset", "document_ids",
                )
            }
            card["evidence_link"] = f"../evidence/{document_path.name}"
            components[entity_id] = Node(
                id=entity_id,
                name=str(entity["name"]),
                component_type="pega_rule",
                file_path=str(document_path),
                relative_path=f"evidence/{document_path.name}",
                depends_on=dependencies[entity_id],
                source_code=json.dumps(card, ensure_ascii=False),
                display_name=str(entity["name"]),
            )
        return components

    def cached_codewiki_components(self, package: dict[str, Any]) -> dict[str, Node]:
        """Reuse the validated compatibility projection for an identical package."""
        cache_path = self.cache.root.parent / "codewiki-components.json"
        adapter_fingerprint = digest(
            inspect.getsource(PegaGraphProvider.codewiki_components)
            + stable_json(sorted(COMPATIBILITY_DEPENDENCY_TYPES))
        )
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if (
                    cached.get("snapshot_key") == package.get("snapshot_key")
                    and cached.get("adapter_fingerprint") == adapter_fingerprint
                    and isinstance(cached.get("components"), dict)
                ):
                    return {
                        component_id: Node.model_validate(record)
                        for component_id, record in cached["components"].items()
                    }
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass

        components = self.codewiki_components(package)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "snapshot_key": package["snapshot_key"],
                    "adapter_fingerprint": adapter_fingerprint,
                    "components": {
                        component_id: component.model_dump(mode="json")
                        for component_id, component in components.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(cache_path)
        return components


class PegaKBGraphProvider(PegaGraphProvider):
    """Adapter for the release-scoped ``pega_kb.mcp_server`` contract.

    The PEGA KB exposes one isolated graph per application release. A provider
    instance selects exactly one release; graph nodes use release-prefixed
    ``uid`` values and reference deterministic Markdown by ``markdown_path``.
    The evidence package retains those node and edge properties while the
    CodeWiki compatibility view is derived from CALLS/READS/WRITES edges.
    """

    _PROJECT_TOOLS = frozenset({"catalog", "kb_status", "list_releases"})
    _PAGE_SIZE = 500

    def __init__(
        self,
        transport: ToolTransport | None,
        *,
        project: str,
        release: str = "lead",
        cache_dir: Path,
        source_root: Path | None = None,
        max_pages: int = 30,
    ) -> None:
        # The upstream bundle may be supplied as either the bundle root or the
        # project directory. Resolve Markdown paths against the project tree.
        project_root = source_root
        if project_root is not None:
            project_root = project_root.expanduser().resolve()
            candidates = (
                project_root / "projects" / project,
                project_root / project,
            )
            project_root = next((item for item in candidates if item.is_dir()), project_root)
        super().__init__(
            transport,
            project=project,
            cache_dir=cache_dir,
            source_root=project_root,
            max_pages=max_pages,
        )
        if not release.strip():
            raise ValueError("A PEGA application release or slug is required")
        self.release_selector = release.strip()
        self.release_id: str | None = None
        self.release_slug: str | None = None
        self.release_name: str | None = None
        self._document_paths: dict[str, str] = {}
        self._document_entities: dict[str, tuple[str, ...]] = {}
        self._node_rows: dict[str, dict[str, Any]] = {}

    async def _call(self, tool_name: str, **arguments: Any) -> Any:
        if self.transport is None:
            raise RuntimeError("This Pega provider is a frozen snapshot with no live MCP transport")
        arguments = {"project": self.project, **arguments}
        if tool_name not in self._PROJECT_TOOLS:
            arguments["release"] = self.release_id or self.release_selector
        result = await self.transport.call(tool_name, arguments)
        self._requests.append({"tool": tool_name, "arguments": arguments})
        if result is not None and not isinstance(result, (dict, list)):
            raise TypeError(f"Pega MCP {tool_name} returned {type(result).__name__}, expected object or list")
        return result

    @staticmethod
    def _rows(value: Any, *, tool: str) -> list[dict[str, Any]]:
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise ValueError(f"Pega MCP {tool} did not return a list of objects")
        return value

    async def initialize(self, *, lightweight: bool = False) -> None:
        catalog = self._rows(await self._call("catalog"), tool="catalog")
        project_rows = [row for row in catalog if row.get("project") == self.project]
        if len(project_rows) != 1:
            known = sorted(str(row.get("project")) for row in catalog)
            raise ValueError(f"Project {self.project!r} is not in catalog; available projects: {known}")

        releases = self._rows(await self._call("list_releases"), tool="list_releases")
        matches = [
            row for row in releases
            if self.release_selector in {str(row.get("release")), str(row.get("slug"))}
        ]
        if len(matches) != 1:
            known = [
                {"release": row.get("release"), "slug": row.get("slug"), "name": row.get("name")}
                for row in releases
            ]
            raise ValueError(
                f"Release {self.release_selector!r} resolves to {len(matches)} releases in "
                f"{self.project}; available releases: {known}"
            )
        selected = matches[0]
        self.release_id = str(selected["release"])
        self.release_slug = str(selected.get("slug") or self.release_selector)
        self.release_name = str(selected.get("name") or self.release_slug)

        statuses = self._rows(await self._call("kb_status"), tool="kb_status")
        status_matches = [row for row in statuses if row.get("release") == self.release_id]
        if len(status_matches) != 1:
            raise ValueError(f"kb_status has no unique manifest for release {self.release_id}")
        status = status_matches[0]
        self.status = self._normalize_status(status)
        self.schema = {
            "adapter": "pega_kb.mcp_server",
            "project": self.project,
            "release": self.release_id,
            "slug": self.release_slug,
            "node_labels": ["Scoped", "Rule", "ExternalRule", "Ruleset", "ApplicationRelease"],
            "edge_types": [
                "CALLS", "READS", "WRITES", "APPLIES_TO_CLASS", "DERIVES_FROM_CLASS",
                "INCLUDES_RULESET", "DEPENDS_ON", "HAS_PAGE_CLASS", "HAS_CLASS_ROOT",
                "HAS_CASE_TYPE", "INSTANCE_OF_DATA_CLASS",
            ],
        }
        self._require_ready_status()

    def _normalize_status(self, row: dict[str, Any]) -> dict[str, Any]:
        status = dict(row)
        status.update(
            {
                "project_id": self.project,
                "projection_status": row.get("status"),
                "projection_manifest_hash": row.get("content_hash"),
                "entities": int(row.get("nodes") or 0),
                "relationships": int(row.get("edges") or 0),
                # Filled from this release's node inventory during capture.
                "documents": None,
            }
        )
        return status

    async def load_schema(self) -> dict[str, Any]:
        if self.status is None or self.release_id is None:
            raise RuntimeError("Call initialize() before loading the selected PEGA release")
        assert self.schema is not None
        return self.schema

    def _require_initialized(self) -> None:
        if self.status is None or self.schema is None or self.release_id is None:
            raise RuntimeError("Call initialize() before Pega retrieval")

    def _require_ready_status(self) -> None:
        if self.status is None:
            raise RuntimeError("Call initialize() before checking Pega KB readiness")
        if self.status.get("status") != "ready":
            raise RuntimeError(
                f"PEGA release {self.release_id or self.release_selector!r} is "
                f"{self.status.get('status')!r}; wait until it is ready"
            )

    async def _read_cypher(
        self, query: str, *, params: dict[str, Any] | None = None, limit: int = _PAGE_SIZE
    ) -> list[dict[str, Any]]:
        result = await self._call("read_cypher", query=query, params=params or {}, limit=limit)
        if not isinstance(result, dict) or not isinstance(result.get("rows"), list):
            raise ValueError("Pega MCP read_cypher returned an invalid rows payload")
        if result.get("truncated"):
            raise RuntimeError("Pega read_cypher result exceeded the requested limit")
        rows = result["rows"]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("Pega MCP read_cypher returned a non-object row")
        return rows

    async def _release_status(self) -> dict[str, Any]:
        rows = self._rows(await self._call("kb_status"), tool="kb_status")
        matches = [row for row in rows if row.get("release") == self.release_id]
        if len(matches) != 1:
            raise RuntimeError(f"Release manifest disappeared for {self.release_id}")
        return self._normalize_status(matches[0])

    async def _verify_status_unchanged(self) -> dict[str, Any]:
        if self.status is None:
            raise RuntimeError("Call initialize() before Pega capture")
        latest = await self._release_status()
        fields = ("status", "nodes", "edges", "content_hash", "slug")
        for key in fields:
            if latest.get(key) != self.status.get(key):
                raise RuntimeError(f"PEGA release {self.release_id} {key} changed during evidence capture")
        latest["documents"] = self.status.get("documents")
        self.status = latest
        self._require_ready_status()
        return latest

    def _source_path(self, source_path: str) -> Path:
        if self.cache.source_root is None:
            raise ValueError("PEGA project source root is required to read referenced Markdown")
        candidate = Path(source_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.cache.source_root / candidate
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file() or resolved.suffix.casefold() != ".md":
            raise ValueError(f"Pega markdown_path is not a Markdown file: {resolved}")
        try:
            resolved.relative_to(self.cache.source_root)
        except ValueError as exc:
            raise ValueError(f"Pega markdown_path escapes the selected project root: {resolved}") from exc
        return resolved

    async def project_projection_tokens(self) -> list[str]:
        """Fingerprint the selected release graph manifest and all referenced Markdown bytes."""
        self._require_initialized()
        assert self.status is not None and self.release_id is not None
        rows = await self._read_cypher(
            "MATCH (n:Scoped) WHERE n.release = $release "
            "RETURN n.uid AS uid, n.markdown_path AS markdown_path ORDER BY n.uid",
            limit=self._PAGE_SIZE,
        )
        expected_nodes = int(self.status["entities"])
        if len(rows) != expected_nodes:
            raise RuntimeError(
                f"Release node inventory changed: read {len(rows)} nodes, "
                f"manifest reports {expected_nodes}"
            )
        markdown_hashes: dict[str, str] = {}
        for row in rows:
            path = row.get("markdown_path")
            if not path:
                continue
            resolved = self._source_path(str(path))
            markdown_hashes[str(path)] = digest(resolved.read_text(encoding="utf-8"))
        revision = digest(
            stable_json(
                {
                    "release": self.release_id,
                    "graph_content_hash": self.status.get("content_hash"),
                    "markdown_sha256": markdown_hashes,
                }
            )
        )
        self.project_manifest_hash = revision
        self.project_revision = revision
        self.project_projection_status = str(self.status.get("status"))
        self.has_project_manifest = True
        self.status["documents"] = len(markdown_hashes)
        return [revision]

    @staticmethod
    def _normalized_node(raw: dict[str, Any], labels: list[str] | None = None) -> dict[str, Any]:
        props = dict(raw)
        node_type = str(props.get("node_type") or "")
        props["id"] = str(props.get("uid") or props.get("id") or "")
        props["name"] = str(props.get("name") or props.get("node_id") or props["id"])
        props["entity_type"] = node_type
        props["class_name"] = str(props.get("class") or props.get("class_name") or "")
        props["is_external"] = node_type == "ExternalRule" or "ExternalRule" in (labels or [])
        props["is_embedded"] = bool(props.get("is_embedded", False))
        if labels:
            props["_labels"] = labels
        return props

    def _remember_entity(self, raw: dict[str, Any], labels: list[str] | None = None) -> PegaEntity:
        normalized = self._normalized_node(raw, labels)
        entity_id = str(normalized.get("id") or "")
        if not entity_id:
            raise ValueError("Pega KB node has no uid")
        document_ids: list[str] = []
        markdown_path = normalized.get("markdown_path")
        if markdown_path:
            document_id = f"{self.project}::document:{digest(entity_id)[:24]}"
            self._document_paths[document_id] = str(markdown_path)
            self._document_entities[document_id] = (entity_id,)
            document_ids.append(document_id)
            self._selected_document_ids.add(document_id)
        entity = PegaEntity.from_result(normalized, document_ids)
        self._entities[entity.id] = entity
        self._node_rows[entity.id] = normalized
        return entity

    def _remember_relationship(self, raw: dict[str, Any]) -> PegaRelationship:
        properties = dict(raw.get("properties") or raw.get("evidence") or {})
        edge_uid = str(properties.get("edge_uid") or raw.get("edge_uid") or raw.get("id") or "")
        source_id = str(raw.get("source_entity_id") or raw.get("source_uid") or "")
        target_id = str(raw.get("target_entity_id") or raw.get("target_uid") or "")
        relation_type = str(raw.get("relationship_type") or raw.get("edge_type") or "")
        if not all((edge_uid, source_id, target_id, relation_type)):
            raise ValueError(f"Incomplete Pega KB edge: {raw!r}")
        relationship_id = f"{self.project}::relationship:{edge_uid}"
        properties.update(
            {
                "id": relationship_id,
                "edge_uid": edge_uid,
                "source_entity_id": source_id,
                "target_entity_id": target_id,
                "relationship_type": relation_type,
            }
        )
        relationship = PegaRelationship.from_result(
            {
                "id": relationship_id,
                "source_entity_id": source_id,
                "target_entity_id": target_id,
                "relationship_type": relation_type,
                "evidence": properties,
            }
        )
        self._relationships[relationship.id] = relationship
        return relationship

    async def search_entities(
        self,
        query: str = "",
        *,
        rule_types: list[str] | None = None,
        class_names: list[str] | None = None,
        rulesets: list[str] | None = None,
        entity_types: list[str] | None = None,
        include_external: bool = False,
    ) -> list[PegaEntity]:
        self._require_initialized()
        result = await self._call("find_rules", name=query, limit=self._PAGE_SIZE)
        rows = self._rows(result, tool="find_rules")
        found: list[PegaEntity] = []
        for row in rows:
            if rule_types and row.get("rule_type") not in rule_types:
                continue
            if class_names and row.get("class") not in class_names:
                continue
            if rulesets and row.get("ruleset") not in rulesets:
                continue
            if entity_types and "Rule" not in entity_types:
                continue
            entity = self._remember_entity(
                {
                    "uid": row.get("uid"),
                    "node_type": "Rule",
                    "name": row.get("name"),
                    "rule_type": row.get("rule_type"),
                    "rule_category": row.get("rule_category"),
                    "class": row.get("class"),
                    "ruleset": row.get("ruleset"),
                    "release": self.release_id,
                    "project": self.project,
                }
            )
            found.append(entity)
        return found

    async def get_entity(self, entity_id: str) -> PegaEntity:
        self._require_initialized()
        self._check_id(entity_id, "entity")
        result = await self._call("get_rule", uid=entity_id, limit=self._PAGE_SIZE)
        if not isinstance(result, dict) or not isinstance(result.get("properties"), dict):
            raise KeyError(f"Pega node {entity_id} was not found")
        props = dict(result["properties"])
        labels = [str(item) for item in props.pop("labels", []) if item]
        entity = self._remember_entity(props, labels)
        if entity.id != entity_id:
            raise ValueError("get_rule returned a different Pega uid")
        rows = await self._read_cypher(
            "MATCH (source:Scoped)-[edge]->(target:Scoped) "
            "WHERE (source.uid = $uid OR target.uid = $uid) "
            "AND source.release = $release AND target.release = $release "
            "RETURN source.uid AS source_entity_id, target.uid AS target_entity_id, "
            "type(edge) AS relationship_type, properties(edge) AS evidence "
            "ORDER BY edge.edge_uid",
            params={"uid": entity_id},
        )
        for row in rows:
            self._remember_relationship(row)
        return entity

    async def traverse(
        self,
        entity_ids: list[str],
        *,
        depth: int = 1,
        direction: str = "both",
        relationship_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        self._require_initialized()
        if not 1 <= depth <= 3 or not 1 <= len(entity_ids) <= 50:
            raise ValueError("Pega KB neighbors supports 1–50 seeds and depth 1–3")
        collected: list[dict[str, Any]] = []
        for entity_id in entity_ids:
            result = await self._call(
                "neighbors",
                uid=entity_id,
                direction=direction,
                edge_types=relationship_types,
                depth=depth,
                limit=self._PAGE_SIZE,
            )
            for row in self._rows(result, tool="neighbors"):
                uid = str(row.get("uid") or "")
                if uid:
                    collected.append({"nodes": [entity_id, uid], "path_document_ids": []})
        return collected

    async def get_document(self, document_id: str) -> PegaDocument:
        self._require_initialized()
        self._check_id(document_id, "document")
        if document_id not in self._selected_document_ids:
            raise ValueError("Document must first be selected by a Pega graph result")
        cached = self.cache.get_document(document_id)
        if cached is not None:
            return cached
        source_path = self._source_path(self._document_paths[document_id])
        markdown = source_path.read_text(encoding="utf-8")
        entity_ids = self._document_entities.get(document_id, ())
        entity = self._entities.get(entity_ids[0]) if entity_ids else None
        document = PegaDocument(
            id=document_id,
            title=entity.name if entity else source_path.stem,
            markdown=markdown,
            source_path=str(source_path),
            entity_ids=tuple(entity_ids),
            properties={
                "release": self.release_id,
                "uid": entity_ids[0] if entity_ids else "",
                "markdown_path": self._document_paths[document_id],
                "source_path": str(source_path),
                "markdown_source": "pega_kb_deterministic_markdown",
            },
        )
        self.cache.put_document(document)
        return document

    async def _capture_documents(self, document_ids: set[str]) -> None:
        if len(document_ids) > 500:
            raise RuntimeError(f"Release selected {len(document_ids)} Markdown files; safety limit is 500")
        for document_id in sorted(document_ids):
            await self.get_document(document_id)

    def _package(self, scope: dict[str, Any], entities: list[PegaEntity], relationships: list[PegaRelationship]) -> dict[str, Any]:
        assert self.status is not None and self.schema is not None
        records = [
            {
                "id": relation.id,
                "source_entity_id": relation.source_entity_id,
                "target_entity_id": relation.target_entity_id,
                "relationship_type": relation.relationship_type,
                "document_id": relation.document_id,
                "properties": relation.properties,
            }
            for relation in sorted(relationships, key=lambda item: item.id)
        ]
        unresolved: list[dict[str, Any]] = []
        for entity in entities:
            props = entity.properties
            for family in ("external", "gap"):
                call_sites = props.get(f"{family}_call_site_ids") or []
                targets = props.get(f"{family}_target_names") or []
                outcomes = props.get(f"{family}_outcomes") or []
                for index, call_site in enumerate(call_sites):
                    unresolved.append(
                        {
                            "source_entity_id": entity.id,
                            "call_site_id": call_site,
                            "target_name": targets[index] if index < len(targets) else None,
                            "outcome": outcomes[index] if index < len(outcomes) else family.upper(),
                        }
                    )
        package = {
            "source_kind": "pega",
            "project_id": self.project,
            "source_contract": {
                "mcp_server": "pega_kb.mcp_server",
                "release": self.release_id,
                "release_slug": self.release_slug,
                "markdown_strategy": "regenerated_deterministic_markdown",
                "legacy_synthesized_markdown": "preserved_as_historical_only",
            },
            "scope": scope | {
                "release": self.release_id,
                "release_slug": self.release_slug,
                "release_name": self.release_name,
                "entity_count": len(entities),
                "relationship_count": len(records),
                "document_count": len(self.cache.manifest()),
                "complete_for_requested_scope": True,
            },
            "status": self.status,
            "schema": self.schema,
            "entities": [
                self._entities[item.id].card() | {"properties": self._entities[item.id].properties}
                for item in sorted(entities, key=lambda value: value.id)
            ],
            "relationships": records,
            "documents": self.cache.manifest(),
            "unresolved_references": unresolved,
            "requests": self._requests,
        }
        package["snapshot_key"] = digest(
            stable_json({key: value for key, value in package.items() if key != "requests"})
        )
        return package

    async def snapshot_slice(
        self,
        *,
        seed_entity_id: str,
        depth: int = 1,
        relationship_types: list[str] | None = None,
        relation_kinds: list[str] | None = None,
        expand_entity_ids: list[str] | None = None,
        max_documents: int = 40,
    ) -> dict[str, Any]:
        self._require_initialized()
        self._require_ready_status()
        self._check_id(seed_entity_id, "entity")
        if not 1 <= depth <= 8:
            raise ValueError("Pega KB capture depth must be between 1 and 8")
        selected_types = set(relationship_types or [])
        selected_kinds = set(relation_kinds or [])
        allowed_types = set((self.schema or {}).get("edge_types") or [])
        if allowed_types and selected_types - allowed_types:
            raise ValueError(f"Relationship types absent from live schema: {sorted(selected_types - allowed_types)}")
        scope_ids = {seed_entity_id}
        frontier = {seed_entity_id}
        for _ in range(depth):
            for entity_id in sorted(frontier):
                await self.get_entity(entity_id)
            # Compute the next layer separately to keep traversal direction and
            # filtering explicit, including neighbors reached from either side.
            next_frontier = set()
            for entity_id in frontier:
                for relation in self._relationships.values():
                    if entity_id not in (relation.source_entity_id, relation.target_entity_id):
                        continue
                    if selected_types and relation.relationship_type not in selected_types:
                        continue
                    if selected_kinds and relation.properties.get("relation_kind") not in selected_kinds:
                        continue
                    neighbor = relation.target_entity_id if relation.source_entity_id == entity_id else relation.source_entity_id
                    if neighbor not in scope_ids:
                        next_frontier.add(neighbor)
            scope_ids.update(next_frontier)
            frontier = next_frontier
            if not frontier:
                break
        expansions = sorted(set(expand_entity_ids or []))
        if not set(expansions).issubset(scope_ids):
            raise ValueError("Expanded entity IDs must first be selected by the seed scope")
        for entity_id in expansions:
            await self.get_entity(entity_id)
            for relation in self._relationships.values():
                if entity_id not in (relation.source_entity_id, relation.target_entity_id):
                    continue
                if selected_types and relation.relationship_type not in selected_types:
                    continue
                if selected_kinds and relation.properties.get("relation_kind") not in selected_kinds:
                    continue
                scope_ids.add(
                    relation.target_entity_id if relation.source_entity_id == entity_id else relation.source_entity_id
                )
        for entity_id in sorted(scope_ids):
            if entity_id not in self._entities:
                await self.get_entity(entity_id)
        relationships = [
            relation for relation in self._relationships.values()
            if relation.source_entity_id in scope_ids and relation.target_entity_id in scope_ids
            and (not selected_types or relation.relationship_type in selected_types)
            and (not selected_kinds or relation.properties.get("relation_kind") in selected_kinds)
        ]
        selected_docs = {
            document_id for entity_id in scope_ids
            for document_id in self._entities[entity_id].document_ids
        }
        if len(selected_docs) > max_documents:
            raise RuntimeError(
                f"Slice selected {len(selected_docs)} Markdown files, over max_documents={max_documents}; "
                "refine the edge scope"
            )
        await self._capture_documents(selected_docs)
        await self._verify_status_unchanged()
        entities = [self._entities[item] for item in sorted(scope_ids)]
        scope = {
            "mode": "focused",
            "seed_entity_ids": [seed_entity_id],
            "depth": depth,
            "relationship_types": sorted(selected_types),
            "relation_kinds": sorted(selected_kinds),
            "release": self.release_id,
            "release_slug": self.release_slug,
            "discovery": "pega_kb_release_bfs",
        }
        if expansions:
            scope["expanded_entity_ids"] = expansions
            scope["expansion_depth"] = 1
        return self._package(scope, entities, relationships)

    async def snapshot_project(self) -> dict[str, Any]:
        """Capture every node, edge, and referenced Markdown document in one release."""
        self._require_initialized()
        self._require_ready_status()
        assert self.status is not None and self.release_id is not None
        expected_nodes = int(self.status["entities"])
        expected_edges = int(self.status["relationships"])
        page_size = min(self._PAGE_SIZE, 500)
        node_rows: list[dict[str, Any]] = []
        for offset in range(0, expected_nodes, page_size):
            node_rows.extend(
                await self._read_cypher(
                    "MATCH (n:Scoped) WHERE n.release = $release "
                    "RETURN properties(n) AS node, labels(n) AS labels ORDER BY n.uid "
                    "SKIP $offset LIMIT $page_size",
                    params={"offset": offset, "page_size": page_size},
                )
            )
        if len(node_rows) != expected_nodes:
            raise RuntimeError(
                f"Release node inventory is incomplete: {len(node_rows)} of {expected_nodes}"
            )
        entities: dict[str, PegaEntity] = {}
        for row in node_rows:
            if not isinstance(row.get("node"), dict):
                raise ValueError("Pega KB node row omitted properties(n)")
            entity = self._remember_entity(row["node"], row.get("labels") or [])
            if entity.id in entities:
                raise RuntimeError(f"Duplicate Pega uid {entity.id}")
            entities[entity.id] = entity
        if any(entity.properties.get("release") != self.release_id for entity in entities.values()):
            raise RuntimeError("Release inventory contains a node from a different application release")

        edge_rows: list[dict[str, Any]] = []
        for offset in range(0, expected_edges, page_size):
            edge_rows.extend(
                await self._read_cypher(
                    "MATCH (source:Scoped)-[edge]->(target:Scoped) "
                    "WHERE edge.release = $release AND source.release = $release AND target.release = $release "
                    "RETURN source.uid AS source_entity_id, target.uid AS target_entity_id, "
                    "type(edge) AS relationship_type, properties(edge) AS evidence "
                    "ORDER BY edge.edge_uid SKIP $offset LIMIT $page_size",
                    params={"offset": offset, "page_size": page_size},
                )
            )
        if len(edge_rows) != expected_edges:
            raise RuntimeError(
                f"Release edge inventory is incomplete: {len(edge_rows)} of {expected_edges}"
            )
        relationships: dict[str, PegaRelationship] = {}
        for row in edge_rows:
            relation = self._remember_relationship(row)
            if relation.id in relationships:
                raise RuntimeError(f"Duplicate Pega relationship {relation.id}")
            if relation.source_entity_id not in entities or relation.target_entity_id not in entities:
                raise RuntimeError(f"Relationship {relation.id} escapes release {self.release_id}")
            if relation.properties.get("release") != self.release_id:
                raise RuntimeError(f"Relationship {relation.id} carries a different release")
            relationships[relation.id] = relation

        document_ids = {
            document_id for entity in entities.values() for document_id in entity.document_ids
        }
        markdown_ref_count = sum(bool(entity.properties.get("markdown_path")) for entity in entities.values())
        if len(document_ids) != markdown_ref_count:
            raise RuntimeError("Markdown references do not map one-to-one to selected release nodes")
        self.status["documents"] = len(document_ids)
        await self._capture_documents(document_ids)
        await self._verify_status_unchanged()
        return self._package(
            {
                "mode": "project",
                "seed_entity_ids": [],
                "relationship_types": [],
                "discovery": "complete_release_inventory",
            },
            list(entities.values()),
            list(relationships.values()),
        )


# Keep all CodeWiki callers on the new bundle contract; historical packages
# remain readable because the evidence records are normalized to the same shape.
PegaGraphProvider = PegaKBGraphProvider


def save_evidence_package(package: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
