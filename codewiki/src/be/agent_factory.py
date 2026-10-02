"""One source-aware tool and prompt factory for root and recursive writers."""

from __future__ import annotations

import json
from typing import Any

from codewiki.src.be.agent_tools.read_code_components import read_code_components_tool
from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor_tool
from codewiki.src.be.agent_tools.pega import read_pega_evidence_tool, retrieve_pega_context_tool
from codewiki.src.be.pega_prompts import (
    PEGA_WRITER_PROMPT, pega_writer_focus, pega_writer_organization,
    pega_writer_requirements,
)
from codewiki.src.be.sources.evidence import digest
from codewiki.src.be.prompt_template import (
    format_leaf_system_prompt,
    format_system_prompt,
    format_user_prompt,
)


def module_agent_spec(
    module_name: str,
    *,
    source_kind: str,
    complex_module: bool,
    custom_instructions: str = "",
    delegation_tool: Any = None,
    pega_specialist_enabled: bool = True,
    doc_type: str | None = None,
) -> tuple[str, list[Any]]:
    if source_kind == "pega":
        specialist_instruction = (
            "Use retrieve_pega_context only for an unresolved identity or cross-rule question "
            "within the captured evidence package. Its searches and traversals cannot expand "
            "the captured graph or document set."
            if pega_specialist_enabled
            else "Use only the captured graph and official Markdown; mark unresolved questions as limits."
        )
        prompt = PEGA_WRITER_PROMPT.format(
            module_name=module_name,
            custom_instructions=custom_instructions or "",
            specialist_instruction=specialist_instruction,
            page_requirements=pega_writer_requirements(doc_type),
            writer_focus=pega_writer_focus(doc_type),
            writer_organization=pega_writer_organization(doc_type),
        )
        tools = [read_pega_evidence_tool, str_replace_editor_tool]
        if pega_specialist_enabled:
            tools.insert(1, retrieve_pega_context_tool)
    else:
        prompt = (
            format_system_prompt(module_name, custom_instructions)
            if complex_module
            else format_leaf_system_prompt(module_name, custom_instructions)
        )
        tools = [read_code_components_tool, str_replace_editor_tool]
    if source_kind != "pega" and complex_module and delegation_tool is not None:
        tools.append(delegation_tool)
    return prompt, tools


def module_user_prompt(
    module_name: str,
    core_component_ids: list[str],
    components: dict[str, Any],
    module_tree: dict[str, Any],
    *,
    source_kind: str,
    pega_provider: Any = None,
) -> str:
    if source_kind != "pega":
        return format_user_prompt(module_name, core_component_ids, components, module_tree)
    if pega_provider is None:
        raise RuntimeError("Pega provider is required for a Pega module")
    cards = []
    for entity_id in core_component_ids:
        component = components.get(entity_id)
        if component is None:
            raise ValueError(f"Unknown Pega component in module {module_name}: {entity_id}")
        cards.append(json.loads(component.source_code or "{}"))
    edges = [
        {
            "id": edge.id,
            "type": edge.relationship_type,
            "relation_kind": edge.properties.get("relation_kind"),
            "step_path": edge.properties.get("step_path"),
            "http_method": edge.properties.get("http_method"),
            "source_entity_id": edge.source_entity_id,
            "target_entity_id": edge.target_entity_id,
            "document_id": edge.document_id,
            "source_location": edge.properties.get("source_location"),
            "condition": edge.properties.get("condition"),
            "qualifiers_json": edge.properties.get("qualifiers_json"),
            "resolution_outcome": edge.properties.get("resolution_outcome"),
            "evidence_link": f"../evidence/edges/{digest(edge.id)[:24]}.md",
        }
        for edge in pega_provider._relationships.values()
        if edge.source_entity_id in core_component_ids or edge.target_entity_id in core_component_ids
    ]
    # A dependency need not own a chapter to matter to the reader. Give the
    # writer the identities at both ends of each selected relationship.
    adjacent_ids = {
        endpoint
        for edge in edges
        for endpoint in (edge["source_entity_id"], edge["target_entity_id"])
        if endpoint not in core_component_ids
    }
    supporting_entities = [
        pega_provider._entities[entity_id].card()
        for entity_id in sorted(adjacent_ids)
        if entity_id in getattr(pega_provider, "_entities", {})
    ]
    unresolved_references = [
        reference
        for reference in getattr(pega_provider, "_unresolved_references", [])
        if reference.get("source_entity_id") in core_component_ids
    ]
    tree_context = []
    purposes = getattr(pega_provider, "_module_purposes", {})

    def visit(branch: dict[str, Any], parent_path: list[str]) -> None:
        for name, info in branch.items():
            children = info.get("children") or {}
            path = [*parent_path, name]
            tree_context.append({
                "name": name, "path": path, "purpose": purposes.get(name, ""),
                "children": list(children),
                "owned_rule_ids": [] if children else info.get("components", []),
            })
            visit(children, path)

    visit(module_tree, [])
    return (
        f"Document the Pega module {module_name}. Create exactly {module_name}.md.\n"
        "The cards identify graph entities and their graph-selected official documents. "
        "Use read_pega_evidence for deterministic configuration and source citations. "
        "The listed relationships are directed configuration links, not execution traces.\n\n"
        f"<MODULE_TREE>{json.dumps(tree_context, ensure_ascii=False)}</MODULE_TREE>\n"
        f"<ENTITY_CARDS>{json.dumps(cards, ensure_ascii=False)}</ENTITY_CARDS>\n"
        f"<DIRECTED_RELATIONSHIPS>{json.dumps(edges, ensure_ascii=False)}</DIRECTED_RELATIONSHIPS>\n"
        f"<SUPPORTING_ENTITIES>{json.dumps(supporting_entities, ensure_ascii=False)}</SUPPORTING_ENTITIES>\n"
        f"<UNRESOLVED_REFERENCES>{json.dumps(unresolved_references, ensure_ascii=False)}</UNRESOLVED_REFERENCES>"
    )
