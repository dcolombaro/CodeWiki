"""Evidence-aware Pega module planning with exact ownership validation."""

from __future__ import annotations

import json
from typing import Any

from codewiki.src.be.module_naming import RESERVED_STEMS, sanitize_module_name
from codewiki.src.be.pega_prompts import PEGA_PLANNER_PROMPT
from codewiki.src.be.sources.pega_mcp import PegaGraphProvider


def documented_rule_ids(package: dict[str, Any]) -> set[str]:
    documents = package.get("documents") or {}
    return {
        str(entity["id"])
        for entity in package.get("entities") or []
        if entity.get("rule_type")
        and not entity.get("is_external")
        and not entity.get("is_embedded")
        and any(document_id in documents for document_id in entity.get("document_ids") or [])
    }


def plan_modules(
    package: dict[str, Any], provider: PegaGraphProvider, backend: Any, cluster_model: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ask for a business tree, then prove exact in-scope rule ownership."""
    scope_ids = documented_rule_ids(package)
    if not scope_ids:
        raise ValueError("The Pega slice has no documented rule entities")
    cards: list[dict[str, Any]] = []
    for entity in package["entities"]:
        if entity["id"] not in scope_ids:
            continue
        card = {
            key: entity.get(key)
            for key in ("id", "name", "entity_type", "rule_type", "rule_category", "class_name", "ruleset", "document_ids")
        }
        for document_id in entity.get("document_ids") or []:
            document = provider.cache.get_document(document_id)
            if document is None:
                continue
            for heading in ("Functional synthesis", "Extracted configuration"):
                try:
                    purpose, line = document.section(heading, max_chars=900)
                    card["semantic_excerpt"] = purpose
                    card["semantic_citation"] = f"{document_id} line {line}"
                    card["semantic_section"] = heading
                    card["semantic_authority"] = (
                        "upstream_llm_interpretation"
                        if heading == "Functional synthesis"
                        else "extracted_configuration"
                    )
                    break
                except KeyError:
                    continue
            if card.get("semantic_excerpt"):
                break
        cards.append(card)
    relation_cards = [
        {
            "id": edge["id"],
            "type": edge["relationship_type"],
            "source_entity_id": edge["source_entity_id"],
            "target_entity_id": edge["target_entity_id"],
            "condition": edge["properties"].get("condition"),
            "qualifiers_json": edge["properties"].get("qualifiers_json"),
            "resolution_outcome": edge["properties"].get("resolution_outcome"),
        }
        for edge in package.get("relationships") or []
    ]
    prompt = PEGA_PLANNER_PROMPT.format(
        project=package["project_id"],
        cards=json.dumps(cards, ensure_ascii=False),
        relationships=json.dumps(relation_cards, ensure_ascii=False),
    )
    answer = backend.complete(prompt, model=cluster_model)
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError("Pega planner model returned an empty response")
    cleaned = answer.strip()
    if cleaned.startswith("```json") and cleaned.endswith("```"):
        cleaned = cleaned.removeprefix("```json").removesuffix("```").strip()
    proposed = json.loads(cleaned)
    return validate_plan(package, proposed)


def validate_plan(
    package: dict[str, Any], proposed: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a generated or supplied plan against the captured graph scope."""
    if not isinstance(proposed, dict):
        raise ValueError("Pega planner must return a JSON object")
    if proposed.get("source_kind") not in (None, "pega"):
        raise ValueError("Pega plan has a different source kind")
    if proposed.get("project_id") not in (None, package["project_id"]):
        raise ValueError("Pega plan belongs to a different project")
    if proposed.get("snapshot_key") not in (None, package["snapshot_key"]):
        raise ValueError("Pega plan belongs to a different evidence snapshot")
    modules = proposed.get("modules")
    if modules is None and isinstance(proposed.get("module_tree"), dict):
        # Older validated plan.json artifacts contain the CodeWiki tree but no
        # proposal list. Reconstruct the proposal, then apply all checks below.
        rationale_map = proposed.get("rationale") or {}
        if not isinstance(rationale_map, dict):
            raise ValueError("Pega plan rationale must be an object")
        modules = [
            {
                "name": name,
                "purpose": rationale_map.get(name, ""),
                "entity_ids": info.get("components") if isinstance(info, dict) else None,
            }
            for name, info in proposed["module_tree"].items()
        ]
    if not isinstance(modules, list):
        raise ValueError("Pega planner must return a JSON object with modules")
    scope_ids = documented_rule_ids(package)
    tree: dict[str, Any] = {}
    ownership: dict[str, str] = {}
    rationale: dict[str, str] = {}
    canonical_modules: list[dict[str, Any]] = []
    for item in modules:
        if not isinstance(item, dict) or not isinstance(item.get("entity_ids"), list):
            raise ValueError("Every Pega module needs an entity_ids list")
        requested_name = str(item.get("name") or "")
        name = sanitize_module_name(requested_name)
        if name != requested_name or name.casefold() in {key.casefold() for key in tree} or name.casefold() in RESERVED_STEMS:
            raise ValueError(f"Unsafe or duplicate Pega module name: {requested_name!r}")
        ids = item["entity_ids"]
        if not ids:
            raise ValueError(f"Pega module {name} is empty")
        for entity_id in ids:
            if entity_id not in scope_ids:
                raise ValueError(f"Unknown or unsupported Pega owner ID: {entity_id!r}")
            if entity_id in ownership:
                raise ValueError(f"Pega rule {entity_id} has two owners")
            ownership[entity_id] = name
        tree[name] = {"components": ids, "children": {}}
        rationale[name] = str(item.get("purpose") or "")
        canonical_modules.append(
            {"name": name, "purpose": rationale[name], "entity_ids": list(ids)}
        )
    missing = sorted(scope_ids - ownership.keys())
    if missing:
        raise ValueError(f"Pega planner omitted {len(missing)} documented rules: {missing}")
    if proposed.get("primary_owner") is not None and proposed["primary_owner"] != ownership:
        raise ValueError("Pega plan ownership map disagrees with its modules")
    if proposed.get("module_tree") is not None and proposed["module_tree"] != tree:
        raise ValueError("Pega plan module tree disagrees with its modules")
    if proposed.get("scope_rule_ids") is not None and proposed["scope_rule_ids"] != sorted(scope_ids):
        raise ValueError("Pega plan scope rule IDs disagree with the evidence snapshot")
    plan = {
        "source_kind": "pega",
        "project_id": package["project_id"],
        "snapshot_key": package["snapshot_key"],
        "scope_rule_ids": sorted(scope_ids),
        "primary_owner": ownership,
        "rationale": rationale,
        "modules": canonical_modules,
        "excluded_rule_ids": [],
        "supporting_entities": {
            str(entity["id"]): (
                "external_reference"
                if entity.get("is_external")
                else "embedded_entity"
                if entity.get("is_embedded")
                else "missing_official_document"
                if entity.get("rule_type")
                else "ontology_or_context"
            )
            for entity in package.get("entities") or []
            if entity["id"] not in scope_ids
        },
        "module_tree": tree,
    }
    return tree, plan
