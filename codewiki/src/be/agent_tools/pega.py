"""Bounded Pega evidence tools for CodeWiki module writers and specialist."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext, Tool

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.backend import usage_to_dict
from codewiki.src.be.llm_services import create_fallback_models
from codewiki.src.be.pega_prompts import PEGA_RETRIEVER_PROMPT
from codewiki.src.be.sources.evidence import digest
from codewiki.src.be.sources.pega_mcp import PegaGraphProvider


def _render_document(
    provider: PegaGraphProvider,
    document_id: str,
    section: str | None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    document = provider.cache.get_document(document_id)
    if document is None:
        raise KeyError(f"Document {document_id} is not cached; select it through the graph first")
    link = f"../evidence/{provider.cache.markdown_path(document_id).name}"
    # A writer may keep the section name when following a continuation line.
    # The explicit line window takes precedence in that case.
    if section and start_line is None:
        start_line, section_end = document.section_bounds(section)
        end_line = section_end
    if start_line is not None:
        lines = document.markdown.splitlines()
        if not 1 <= start_line <= len(lines):
            raise ValueError(f"start_line must be between 1 and {len(lines)}")
        last_line = min(end_line or len(lines), len(lines), start_line + 159)
        if last_line < start_line:
            raise ValueError("end_line must be at least start_line")
        selected: list[str] = []
        size = 0
        actual_end = start_line - 1
        for number in range(start_line, last_line + 1):
            rendered = f"L{number}: {lines[number - 1]}"
            if size + len(rendered) > 12000 and selected:
                break
            selected.append(rendered)
            size += len(rendered)
            actual_end = number
        continuation = (
            f"\nMore lines in requested span; continue with start_line={actual_end + 1}."
            if actual_end < (end_line or len(lines))
            else ""
        )
        return (
            f"Document {document_id}, [local evidence]({link}), "
            f"lines {start_line}-{actual_end}\n\n" + "\n".join(selected) + continuation
        )
    headings = [
        f"line {index}: {text}"
        for index, text in enumerate(document.markdown.splitlines(), 1)
        if text.startswith("#")
    ]
    return f"Document {document_id}, [local evidence]({link})\nAvailable sections:\n" + "\n".join(headings)


async def read_pega_evidence(
    ctx: RunContext[CodeWikiDeps],
    document_id: str,
    section: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read a named section of a graph-selected official Pega document.

    Omit section and line range to list headings. Long sections return a
    continuation line; use start_line and end_line to read later steps.
    Use an exact document ID from a module card, graph result, or specialist answer.
    """
    provider = ctx.deps.pega_provider
    if provider is None:
        raise RuntimeError("Pega provider is unavailable")
    if document_id not in provider._selected_document_ids:
        available = ", ".join(sorted(provider._selected_document_ids))
        return (
            f"Document {document_id} was not selected by the captured graph. "
            f"Use an exact document_id from the entity cards. "
            f"Selected document IDs: {available}"
        )
    try:
        if provider.cache.get_document(document_id) is None:
            await provider.get_document(document_id)
        rendered = _render_document(provider, document_id, section, start_line, end_line)
    except (KeyError, ValueError) as exc:
        return f"Could not read document {document_id}: {exc}. Request its headings or a valid line window."
    provider._evidence_reads.append(
        {
            "tool": "read_pega_evidence",
            "module": ctx.deps.current_module_name,
            "document_id": document_id,
            "section": section,
            "start_line": start_line,
            "end_line": end_line,
            "returned_sha256": digest(rendered),
            "returned_chars": len(rendered),
        }
    )
    return rendered


read_pega_evidence_tool = Tool(
    function=read_pega_evidence,
    name="read_pega_evidence",
    takes_ctx=True,
)


@dataclass
class SpecialistDeps:
    provider: PegaGraphProvider
    module_name: str = ""
    remaining_calls: int = 8
    partial: bool = False

    def spend(self) -> bool:
        if self.remaining_calls <= 0:
            self.partial = True
            return False
        self.remaining_calls -= 1
        return True


def _resolve_captured_entity_id(provider: PegaGraphProvider, value: str) -> str | None:
    """Resolve a model-supplied short rule ID only within the captured package."""
    if value in provider._entities:
        return value
    matches = []
    for entity_id, entity in provider._entities.items():
        properties = entity.properties or {}
        candidates = {
            entity_id,
            entity_id.rsplit("|", 1)[-1],
            entity_id.rsplit(":", 1)[-1],
            str(properties.get("uid") or ""),
            str(properties.get("node_id") or ""),
            str(properties.get("uid") or "").rsplit(":", 1)[-1],
            str(properties.get("node_id") or "").rsplit(":", 1)[-1],
        }
        if value in candidates:
            matches.append(entity_id)
    return matches[0] if len(matches) == 1 else None


def _resolve_captured_relationship_id(provider: PegaGraphProvider, value: str) -> str | None:
    if value in provider._relationships:
        return value
    matches = []
    for relationship_id, edge in provider._relationships.items():
        properties = edge.properties or {}
        candidates = {
            relationship_id,
            relationship_id.rsplit("|", 1)[-1],
            relationship_id.rsplit("::", 1)[-1],
            relationship_id.rsplit(":", 1)[-1],
            str(properties.get("edge_uid") or ""),
            f"relationship:{properties.get('edge_uid')}" if properties.get("edge_uid") else "",
            str(properties.get("edge_uid") or "").rsplit(":", 1)[-1],
        }
        if value in candidates:
            matches.append(relationship_id)
    return matches[0] if len(matches) == 1 else None


def _resolve_captured_document_id(provider: PegaGraphProvider, value: str) -> str | None:
    if value in provider._selected_document_ids:
        return value
    matches = [
        document_id
        for document_id in provider._selected_document_ids
        if document_id.rsplit("::", 1)[-1] == value
        or document_id.rsplit(":", 1)[-1] == value
        or document_id.endswith(value)
    ]
    return matches[0] if len(matches) == 1 else None


async def specialist_search(
    ctx: RunContext[SpecialistDeps],
    query: str,
    rule_type: str | None = None,
    class_name: str | None = None,
    ruleset: str | None = None,
) -> str:
    """Resolve entity identity inside the captured evidence package."""
    if not ctx.deps.spend():
        return '{"partial":true,"reason":"retrieval budget exhausted"}'
    normalized_query = " ".join(query.casefold().split())
    query_terms = normalized_query.split()
    matches = []
    for entity in ctx.deps.provider._entities.values():
        searchable = " ".join(
            (
                entity.id,
                entity.name,
                entity.entity_type,
                entity.rule_type,
                entity.rule_category,
                entity.class_name,
                entity.ruleset,
            )
        ).casefold()
        if normalized_query and normalized_query not in searchable and not all(
            term in searchable for term in query_terms
        ):
            continue
        if rule_type and entity.rule_type.casefold() != rule_type.casefold():
            continue
        if class_name and entity.class_name.casefold() != class_name.casefold():
            continue
        if ruleset and entity.ruleset.casefold() != ruleset.casefold():
            continue
        matches.append(entity)
    matches.sort(key=lambda item: (item.name.casefold(), item.id))
    if len(matches) > 20:
        ctx.deps.partial = True
    return json.dumps(
        {"count": len(matches), "candidates": [item.card() for item in matches[:20]], "truncated": len(matches) > 20},
        ensure_ascii=False,
    )


async def specialist_inspect(ctx: RunContext[SpecialistDeps], entity_id: str) -> str:
    """Inspect one exact entity and its directed relationships in the captured package."""
    if not ctx.deps.spend():
        return '{"partial":true,"reason":"retrieval budget exhausted"}'
    provider = ctx.deps.provider
    resolved_id = _resolve_captured_entity_id(provider, entity_id)
    entity = provider._entities.get(resolved_id) if resolved_id else None
    if entity is None or resolved_id is None:
        return json.dumps(
            {"error": "entity_not_in_captured_evidence_package", "entity_id": entity_id}
        )
    edges = [
        {
            "id": edge.id,
            "source_entity_id": edge.source_entity_id,
            "target_entity_id": edge.target_entity_id,
            "type": edge.relationship_type,
            "document_id": edge.document_id,
            "properties": edge.properties,
        }
        for edge in ctx.deps.provider._relationships.values()
        if resolved_id in (edge.source_entity_id, edge.target_entity_id)
    ]
    if len(edges) > 100:
        ctx.deps.partial = True
    return json.dumps(
        {"entity": entity.card(), "edges": edges[:100], "truncated": len(edges) > 100},
        ensure_ascii=False,
    )


async def specialist_traverse(
    ctx: RunContext[SpecialistDeps],
    entity_ids: list[str],
    relationship_types: list[str] | None = None,
    depth: int = 1,
) -> str:
    """Follow a bounded route using only relationships in the captured package."""
    if not ctx.deps.spend():
        return '{"partial":true,"reason":"retrieval budget exhausted"}'
    provider = ctx.deps.provider
    if not entity_ids or len(entity_ids) > 50 or not 1 <= depth <= 8:
        return json.dumps({"error": "invalid_bounded_traversal_parameters"})
    resolved_ids = [_resolve_captured_entity_id(provider, item) for item in entity_ids]
    if any(entity_id is None for entity_id in resolved_ids):
        return json.dumps({"error": "seed_entity_not_in_captured_evidence_package"})
    entity_ids = [item for item in resolved_ids if item is not None]
    selected_types = set(relationship_types or [])
    adjacency: dict[str, list[tuple[str, Any]]] = {}
    for edge in provider._relationships.values():
        if (
            selected_types
            and edge.relationship_type not in selected_types
            and edge.properties.get("relation_kind") not in selected_types
        ):
            continue
        adjacency.setdefault(edge.source_entity_id, []).append((edge.target_entity_id, edge))
        adjacency.setdefault(edge.target_entity_id, []).append((edge.source_entity_id, edge))
    for links in adjacency.values():
        links.sort(key=lambda item: (item[1].id, item[0]))

    paths: list[dict[str, Any]] = []
    frontier = [(seed, (seed,), (), ()) for seed in sorted(set(entity_ids))]
    truncated = False
    for distance in range(1, depth + 1):
        next_frontier = []
        for current, entity_path, relation_path, document_path in frontier:
            for neighbor, edge in adjacency.get(current, []):
                if neighbor in entity_path:
                    continue
                edge_documents = (edge.document_id,) if edge.document_id else ()
                next_path_documents = tuple(dict.fromkeys((*document_path, *edge_documents)))
                path = {
                    "path_entity_ids": [*entity_path, neighbor],
                    "path_relationship_ids": [*relation_path, edge.id],
                    "path_document_ids": list(next_path_documents),
                    "distance": distance,
                }
                if len(paths) < 500:
                    paths.append(path)
                    next_frontier.append(
                        (
                            neighbor,
                            (*entity_path, neighbor),
                            (*relation_path, edge.id),
                            next_path_documents,
                        )
                    )
                else:
                    truncated = True
                    break
            if truncated:
                break
        if truncated or not next_frontier:
            break
        frontier = next_frontier
    if len(paths) > 40 or truncated:
        ctx.deps.partial = True
    compact = [
        {
            "path_entity_ids": path.get("path_entity_ids"),
            "path_relationship_ids": path.get("path_relationship_ids"),
            "path_document_ids": path.get("path_document_ids"),
            "distance": path.get("distance"),
        }
        for path in paths[:40]
    ]
    return json.dumps(
        {"count": len(paths), "paths": compact, "truncated": len(paths) > 40 or truncated}
    )


async def specialist_document(
    ctx: RunContext[SpecialistDeps],
    document_id: str,
    section: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read a Markdown document selected into the captured evidence package."""
    if not ctx.deps.spend():
        return '{"partial":true,"reason":"retrieval budget exhausted"}'
    if document_id not in ctx.deps.provider._selected_document_ids:
        return json.dumps(
            {"error": "document_not_selected_by_captured_evidence_package", "document_id": document_id}
        )
    if ctx.deps.provider.cache.get_document(document_id) is None:
        await ctx.deps.provider.get_document(document_id)
    rendered = _render_document(ctx.deps.provider, document_id, section, start_line, end_line)
    ctx.deps.provider._evidence_reads.append(
        {
            "tool": "specialist_document",
            "module": ctx.deps.module_name,
            "document_id": document_id,
            "section": section,
            "start_line": start_line,
            "end_line": end_line,
            "returned_sha256": digest(rendered),
            "returned_chars": len(rendered),
        }
    )
    return rendered


class SpecialistCitation(BaseModel):
    document_id: str
    section: str
    supports: str


class SpecialistAnswer(BaseModel):
    summary: str
    entity_ids: list[str]
    relationship_ids: list[str]
    citations: list[SpecialistCitation]
    uncertainties: list[str]
    complete_for_question: bool


async def retrieve_pega_context(ctx: RunContext[CodeWikiDeps], question: str) -> str:
    """Ask a bounded Pega specialist to resolve a cross-rule documentation question."""
    provider = ctx.deps.pega_provider
    if provider is None:
        raise RuntimeError("Pega provider is unavailable")
    specialist = Agent(
        create_fallback_models(ctx.deps.config),
        name="pega-evidence-specialist",
        deps_type=SpecialistDeps,
        output_type=SpecialistAnswer,
        tools=[
            Tool(specialist_search, name="search_pega_entities", takes_ctx=True),
            Tool(specialist_inspect, name="inspect_pega_entity", takes_ctx=True),
            Tool(specialist_traverse, name="traverse_pega_graph", takes_ctx=True),
            Tool(specialist_document, name="read_pega_document", takes_ctx=True),
        ],
        system_prompt=PEGA_RETRIEVER_PROMPT,
    )
    specialist_deps = SpecialistDeps(provider, module_name=ctx.deps.current_module_name)
    started = time.perf_counter()
    result = await specialist.run(question, deps=specialist_deps)
    usage = getattr(result, "usage", None)
    if callable(usage):
        usage = usage()
    provider._specialist_events.append(
        {
            "question": question,
            "module": ctx.deps.current_module_name,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "usage": usage_to_dict(usage),
            "partial": specialist_deps.partial,
            "retrieval_scope": "captured_evidence_package",
        }
    )
    answer = result.output
    if specialist_deps.partial:
        answer.complete_for_question = False
        answer.uncertainties.append("The bounded retrieval returned partial evidence.")
    canonical_entities = []
    invalid_references: list[str] = []
    for entity_id in answer.entity_ids:
        resolved = _resolve_captured_entity_id(provider, entity_id)
        if resolved is None:
            invalid_references.append(f"entity {entity_id}")
        else:
            canonical_entities.append(resolved)
    answer.entity_ids = canonical_entities
    canonical_relationships = []
    for relation_id in answer.relationship_ids:
        resolved = _resolve_captured_relationship_id(provider, relation_id)
        if resolved is None:
            invalid_references.append(f"relationship {relation_id}")
        else:
            canonical_relationships.append(resolved)
    answer.relationship_ids = canonical_relationships
    verified_citations = []
    for citation in answer.citations:
        resolved_document_id = _resolve_captured_document_id(provider, citation.document_id)
        if resolved_document_id is None:
            invalid_references.append(f"unselected or ambiguous document {citation.document_id}")
            continue
        citation.document_id = resolved_document_id
        document = provider.cache.get_document(resolved_document_id)
        if document is None:
            invalid_references.append(f"unread document {citation.document_id}")
            continue
        try:
            document.section_bounds(citation.section)
        except KeyError:
            invalid_references.append(
                f"absent section {citation.section!r} in {citation.document_id}"
            )
            continue
        verified_citations.append(citation)
    answer.citations = verified_citations
    if invalid_references:
        answer.complete_for_question = False
        answer.summary = "The specialist could not verify its full cross-rule answer against captured evidence."
        answer.uncertainties.append(
            "Unverified references were removed: " + "; ".join(invalid_references)
        )
        provider._specialist_events[-1]["partial"] = True
    return answer.model_dump_json(indent=2)


retrieve_pega_context_tool = Tool(
    function=retrieve_pega_context,
    name="retrieve_pega_context",
    takes_ctx=True,
)
