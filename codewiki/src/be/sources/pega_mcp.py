"""Project-scoped Pega MCP transport and deterministic evidence acquisition."""

from __future__ import annotations

import json
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
    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None: ...


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

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
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
            if set(structured) == {"result"} and (
                structured["result"] is None or isinstance(structured["result"], dict)
            ):
                return structured["result"]
            return structured
        for item in result.content:
            raw_text = getattr(item, "text", None)
            if not isinstance(raw_text, str):
                continue
            decoded = json.loads(raw_text)
            if decoded is None or isinstance(decoded, dict):
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
        max_pages: int = 30,
    ) -> None:
        if not project.strip():
            raise ValueError("A Pega project ID is required")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self.transport = transport
        self.project = project
        self.cache = EvidenceCache(cache_dir)
        self.max_pages = max_pages
        self.status: dict[str, Any] | None = None
        self.schema: dict[str, Any] | None = None
        self._selected_document_ids: set[str] = set()
        self._entities: dict[str, PegaEntity] = {}
        self._relationships: dict[str, PegaRelationship] = {}
        self._requests: list[dict[str, Any]] = []
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

    async def initialize(self) -> None:
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
        self.schema = await self._call("describe_graph") or {}

    def _require_initialized(self) -> None:
        if self.status is None or self.schema is None:
            raise RuntimeError("Call initialize() before Pega retrieval")

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
        expand_entity_ids: list[str] | None = None,
        max_documents: int = 40,
    ) -> dict[str, Any]:
        """Capture a bounded graph slice and official documents into the cache."""
        self._require_initialized()
        self._check_id(seed_entity_id, "entity")
        if not 1 <= depth <= 8:
            raise ValueError("depth must be between 1 and 8")
        selected_types = set(relationship_types or [])
        schema_types = {
            str(shape.get("relationship_type"))
            for shape in (self.schema or {}).get("relationship_shapes", [])
            if isinstance(shape, dict)
        }
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
        }
        projection_tokens = {
            str(properties["projection_token"])
            for properties in (
                *[self._entities[entity_id].properties for entity_id in scope_ids],
                *[relation.properties for relation in scoped_relationships.values()],
            )
            if properties.get("projection_token")
        }
        if len(projection_tokens) > 1:
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
        package = {
            "source_kind": "pega",
            "project_id": self.project,
            "scope": {
                "seed_entity_ids": [seed_entity_id],
                "depth": depth,
                "relationship_types": sorted(selected_types),
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

    def _check_id(self, value: str, kind: str) -> None:
        if not isinstance(value, str) or not value.startswith(f"{self.project}::"):
            raise ValueError(f"{kind} ID does not belong to project {self.project!r}: {value!r}")

    def load_snapshot(self, path: Path) -> dict[str, Any]:
        """Verify and restore one immutable evidence package from local files."""
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
        self._selected_document_ids.clear()
        self._entities.clear()
        self._relationships.clear()
        for document_id, manifest_entry in documents.items():
            self._check_id(document_id, "document")
            if not isinstance(manifest_entry, dict):
                raise ValueError(f"Invalid document manifest entry for {document_id}")
            self.cache.load_document(document_id, manifest_entry)
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
            and not entity.get("is_external")
            and not entity.get("is_embedded")
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


def save_evidence_package(package: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
