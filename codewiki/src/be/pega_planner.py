"""Evidence-aware Pega module planning with exact ownership validation."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any

from codewiki.src.be.module_naming import RESERVED_STEMS, sanitize_module_name
from codewiki.src.be.pega_prompts import (
    PEGA_PLANNER_PROMPT, PEGA_PROJECT_BATCH_PLANNER_PROMPT,
    PEGA_PROJECT_RECONCILIATION_PROMPT, pega_documentation_brief,
)
from codewiki.src.be.sources.pega_mcp import PegaGraphProvider
from codewiki.src.be.utils import count_tokens


def _planner_cards(
    package: dict[str, Any],
    provider: PegaGraphProvider,
    entity_ids: set[str],
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for entity in package["entities"]:
        if entity["id"] not in entity_ids:
            continue
        card = {
            key: entity.get(key)
            for key in ("id", "name", "entity_type", "rule_type", "rule_category", "class_name", "ruleset", "document_ids")
        }
        for document_id in entity.get("document_ids") or []:
            document = provider.cache.get_document(document_id)
            if document is None:
                continue
            for heading in ("Functional synthesis", "Extracted configuration", "Configurazione"):
                try:
                    section_text, line = document.section(heading)
                    card["semantic_section_text"] = section_text
                    card["semantic_citation"] = f"{document_id} line {line}"
                    card["semantic_section"] = heading
                    card["semantic_authority"] = (
                        "upstream_llm_interpretation"
                        if heading == "Functional synthesis"
                        else "deterministic_extraction"
                        if heading == "Configurazione"
                        else "extracted_configuration"
                    )
                    break
                except KeyError:
                    continue
            if card.get("semantic_section_text"):
                break
        cards.append(card)
    return cards


def _planner_relationship_cards(
    package: dict[str, Any], entity_ids: set[str]
) -> list[dict[str, Any]]:
    bookkeeping_properties = {"document_id", "id", "managed_by", "project_id", "projection_token"}
    cards = []
    for edge in package.get("relationships") or []:
        source_id = edge["source_entity_id"]
        target_id = edge["target_entity_id"]
        if source_id not in entity_ids and target_id not in entity_ids:
            continue
        properties = edge.get("properties") or {}
        card = {
            "id": edge["id"],
            "type": edge["relationship_type"],
            "source_entity_id": source_id,
            "target_entity_id": target_id,
            "document_id": edge.get("document_id") or properties.get("document_id"),
        }
        card.update({
            key: value
            for key, value in properties.items()
            if key not in bookkeeping_properties and value not in (None, "")
        })
        cards.append(card)
    return cards


def _planner_context_cards(
    package: dict[str, Any], entity_ids: set[str]
) -> list[dict[str, Any]]:
    """Describe endpoints adjacent to owners without making them page owners."""
    incident = _planner_relationship_cards(package, entity_ids)
    context_ids = {
        endpoint
        for edge in incident
        for endpoint in (edge["source_entity_id"], edge["target_entity_id"])
        if endpoint not in entity_ids
    }
    entities = {
        entity["id"]: entity
        for entity in [*(package.get("entities") or []), *(package.get("context_entities") or [])]
    }
    cards = []
    for entity_id in sorted(context_ids):
        entity = entities.get(entity_id)
        if entity is None:
            continue
        cards.append({
            "id": entity_id,
            "name": entity.get("name"),
            "entity_type": entity.get("entity_type"),
            "rule_type": entity.get("rule_type"),
            "rule_category": entity.get("rule_category"),
            "class_name": entity.get("class_name"),
            "ruleset": entity.get("ruleset"),
            "is_external": entity.get("is_external"),
            "is_embedded": entity.get("is_embedded"),
            "document_ids": entity.get("document_ids") or [],
            "role": "context_only",
        })
    return cards


def _planner_prompt(
    project: str,
    cards: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    context_cards: list[dict[str, Any]],
    *,
    project_batch: bool,
    documentation_brief: str = "",
) -> str:
    template = PEGA_PROJECT_BATCH_PLANNER_PROMPT if project_batch else PEGA_PLANNER_PROMPT
    return documentation_brief + template.format(
        project=project,
        cards=json.dumps(cards, ensure_ascii=False),
        relationships=json.dumps(relationships, ensure_ascii=False),
        context_cards=json.dumps(context_cards, ensure_ascii=False),
    )


def documented_rule_ids(package: dict[str, Any]) -> set[str]:
    documents = package.get("documents") or {}
    return {
        str(entity["id"])
        for entity in package.get("entities") or []
        if entity.get("rule_type")
        and any(document_id in documents for document_id in entity.get("document_ids") or [])
    }


def plan_modules(
    package: dict[str, Any], provider: PegaGraphProvider, backend: Any, cluster_model: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ask for a business tree, then prove exact in-scope rule ownership."""
    scope_ids = documented_rule_ids(package)
    if not scope_ids:
        raise ValueError("The Pega slice has no documented rule entities")
    project_batch = (package.get("scope") or {}).get("mode") in {"project", "project_batch"}
    documentation_brief = pega_documentation_brief(getattr(backend, "_config", None))
    cards = _planner_cards(package, provider, scope_ids)
    relation_cards = _planner_relationship_cards(package, scope_ids)
    context_cards = _planner_context_cards(package, scope_ids)
    prompt = _planner_prompt(
        package["project_id"], cards, relation_cards, context_cards,
        project_batch=project_batch, documentation_brief=documentation_brief,
    )
    answer = backend.complete(prompt, model=cluster_model)
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError("Pega planner model returned an empty response")
    cleaned = answer.strip()
    if cleaned.startswith("```json") and cleaned.endswith("```"):
        cleaned = cleaned.removeprefix("```json").removesuffix("```").strip()
    proposed = json.loads(cleaned)
    return validate_plan(package, proposed)


def _project_batches(
    package: dict[str, Any],
    provider: PegaGraphProvider,
    *,
    prompt_token_target: int,
    documentation_brief: str = "",
    split_oversized_groups: bool = False,
) -> list[list[str]]:
    """Pack rule groups by prompt size, keeping each rule's evidence intact."""
    selected = documented_rule_ids(package)
    if not selected:
        raise ValueError("The Pega project has no documented rule entities")
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for entity in package["entities"]:
        if entity["id"] in selected:
            groups[(str(entity.get("ruleset") or ""), str(entity.get("class_name") or ""))].append(entity)
    groups_by_ids: list[list[str]] = []
    for group_key in sorted(groups):
        members = sorted(
            groups[group_key],
            key=lambda item: (str(item.get("rule_category") or ""), str(item.get("name") or ""), item["id"]),
        )
        group_ids = [entity["id"] for entity in members]
        if not split_oversized_groups:
            groups_by_ids.append(group_ids)
            continue
        current_group: list[str] = []
        for entity_id in group_ids:
            candidate = [*current_group, entity_id]
            selected_ids = set(candidate)
            candidate_prompt = _planner_prompt(
                package["project_id"],
                _planner_cards(package, provider, selected_ids),
                _planner_relationship_cards(package, selected_ids),
                _planner_context_cards(package, selected_ids),
                project_batch=True,
                documentation_brief=documentation_brief,
            )
            if current_group and count_tokens(candidate_prompt) > prompt_token_target:
                groups_by_ids.append(current_group)
                current_group = [entity_id]
            else:
                current_group = candidate
        if current_group:
            groups_by_ids.append(current_group)

    batches: list[list[str]] = []
    current: list[str] = []
    for group_ids in groups_by_ids:
        candidate = [*current, *group_ids]
        candidate_set = set(candidate)
        candidate_cards = _planner_cards(package, provider, candidate_set)
        candidate_relations = _planner_relationship_cards(package, candidate_set)
        candidate_context = _planner_context_cards(package, candidate_set)
        candidate_prompt = _planner_prompt(
            package["project_id"],
            candidate_cards,
            candidate_relations,
            candidate_context,
            project_batch=True, documentation_brief=documentation_brief,
        )
        if current and count_tokens(candidate_prompt) > prompt_token_target:
            batches.append(current)
            current = list(group_ids)
            # An individual ruleset/class group remains whole even if it is
            # larger than the target; semantic sections are never truncated.
            group_set = set(current)
            group_cards = _planner_cards(package, provider, group_set)
            group_relations = _planner_relationship_cards(package, group_set)
            group_context = _planner_context_cards(package, group_set)
            group_prompt = _planner_prompt(
                package["project_id"], group_cards, group_relations, group_context,
                project_batch=True, documentation_brief=documentation_brief,
            )
            if count_tokens(group_prompt) > prompt_token_target:
                batches.append(current)
                current = []
        else:
            current = candidate
    if current:
        batches.append(current)
    if set().union(*(set(batch) for batch in batches)) != selected:
        raise RuntimeError("Project planner batches do not cover every documented rule")
    return batches


def _reconcile_project_modules(
    package: dict[str, Any], candidates: list[dict[str, Any]],
    backend: Any, cluster_model: str | None, documentation_brief: str = "",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Reassign ownership globally; discovery batch boundaries have no authority."""
    owners = documented_rule_ids(package)
    entities = {entity["id"]: entity for entity in package["entities"]}
    owner_refs = {entity_id: f"R{index:04d}" for index, entity_id in enumerate(sorted(owners), 1)}
    context_refs = {
        entity_id: f"C{index:04d}"
        for index, entity_id in enumerate(sorted(entities.keys() - owners), 1)
    }
    refs = {**owner_refs, **context_refs}

    def catalog(selected: dict[str, str]) -> dict[str, Any]:
        return {
            ref: {
                key: entities[entity_id].get(key)
                for key in ("name", "rule_type", "class_name", "ruleset", "is_external", "is_embedded")
            }
            for entity_id, ref in selected.items()
        }

    relations: Counter = Counter()
    for edge in package.get("relationships") or []:
        source, target = edge["source_entity_id"], edge["target_entity_id"]
        properties = edge.get("properties") or {}
        relations[(
            refs[source], refs[target], edge["relationship_type"],
            str(properties.get("relation_kind") or ""),
            str(properties.get("resolution_outcome") or ""),
        )] += 1
    prompt = documentation_brief + PEGA_PROJECT_RECONCILIATION_PROMPT.format(
        project=package["project_id"],
        rules=json.dumps(catalog(owner_refs), ensure_ascii=False),
        context=json.dumps(catalog(context_refs), ensure_ascii=False),
        candidates=json.dumps([
            {"name": item["name"], "purpose": item["purpose"],
             "rule_refs": [owner_refs[entity_id] for entity_id in item["entity_ids"]]}
            for item in candidates
        ], ensure_ascii=False),
        relationships=json.dumps([
            {"source": key[0], "target": key[1], "type": key[2],
             "relation_kind": key[3], "resolution_outcome": key[4], "count": count}
            for key, count in sorted(relations.items())
        ], ensure_ascii=False),
    )
    ids_by_ref = {ref: entity_id for entity_id, ref in owner_refs.items()}

    def expand(items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            raise ValueError("Reconciled modules/children must be a list")
        expanded = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("A reconciled module must be an object")
            module = {"name": item.get("name"), "purpose": item.get("purpose")}
            if item.get("children"):
                if item.get("rule_refs") or item.get("entity_ids"):
                    raise ValueError("Only leaves may own rules; parent pages summarize children")
                module["children"] = expand(item["children"])
            else:
                rule_refs = item.get("rule_refs")
                if not isinstance(rule_refs, list):
                    raise ValueError("A leaf must supply rule_refs")
                unknown = [ref for ref in rule_refs if not isinstance(ref, str) or ref not in ids_by_ref]
                if unknown:
                    raise ValueError(f"Unknown or context-only owner refs: {unknown}")
                module["entity_ids"] = [ids_by_ref[ref] for ref in rule_refs]
            expanded.append(module)
        return expanded

    errors: list[str] = []
    for attempt in range(2):
        feedback = (
            "\nYour previous proposal failed validation. Return a complete corrected hierarchy. "
            "Validation error (IDs map to refs in RULE_CATALOG): " + errors[-1]
            if errors else ""
        )
        answer = backend.complete(prompt + feedback, model=cluster_model)
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("Project reconciliation returned an empty response")
        cleaned = answer.strip()
        if cleaned.startswith("```json") and cleaned.endswith("```"):
            cleaned = cleaned.removeprefix("```json").removesuffix("```").strip()
        try:
            proposed = json.loads(cleaned)
            if not isinstance(proposed, dict):
                raise ValueError("Project reconciliation must return an object")
            tree, plan = validate_plan(package, {"modules": expand(proposed.get("modules"))})
            return tree, plan, {
                "global_prompt_tokens": count_tokens(prompt),
                "reconciliation_attempts": attempt + 1,
                "reconciliation_validation_errors": errors,
            }
        except (ValueError, KeyError) as exc:
            message = str(exc)
            for ref, entity_id in ids_by_ref.items():
                message = message.replace(entity_id, ref)
            errors.append(message)
    raise ValueError("Project hierarchy failed validation; page generation stopped: " + errors[-1])


def plan_project_modules(
    package: dict[str, Any], provider: PegaGraphProvider, backend: Any, cluster_model: str | None,
    *, prompt_token_target: int | None = None, split_oversized_groups: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Discover capabilities in batches, then reconcile a hierarchy across the project."""
    usage_start = len(getattr(backend, "usage_events", []))
    prompt_token_target = (
        int(prompt_token_target) if prompt_token_target is not None
        else int(getattr(getattr(backend, "_config", None), "max_token_per_module", 36_369))
    )
    documentation_brief = pega_documentation_brief(getattr(backend, "_config", None))
    batches = _project_batches(
        package, provider, prompt_token_target=max(1, prompt_token_target),
        documentation_brief=documentation_brief,
        split_oversized_groups=split_oversized_groups,
    )
    all_entities = {entity["id"]: entity for entity in package["entities"]}
    proposed_modules: list[dict[str, Any]] = []
    used_names: set[str] = set()
    fallback_batches: list[dict[str, Any]] = []
    for batch_number, batch in enumerate(batches, 1):
        selected = set(batch)
        incident_edges = [
            edge for edge in package["relationships"]
            if edge["source_entity_id"] in selected or edge["target_entity_id"] in selected
        ]
        context_ids = {
            endpoint
            for edge in incident_edges
            for endpoint in (edge["source_entity_id"], edge["target_entity_id"])
            if endpoint not in selected
        }
        partial = {
            **package,
            "scope": {**package["scope"], "mode": "project_batch"},
            "entities": [all_entities[entity_id] for entity_id in batch],
            "relationships": incident_edges,
            "context_entities": [all_entities[entity_id] for entity_id in sorted(context_ids)],
        }
        class_name = str(all_entities[batch[0]].get("class_name") or "Project")
        subject = sanitize_module_name("_".join(class_name.split("-")[-2:]))[:40]
        try:
            _, batch_plan = plan_modules(partial, provider, backend, cluster_model)
        except (ValueError, KeyError) as exc:
            # Preserve complete ownership when a model omits or repeats an ID.
            # The explicit fallback remains visible in plan.json for review.
            fallback_batches.append({"batch": batch_number, "reason": str(exc)[:300]})
            batch_plan = {"modules": [{
                "name": sanitize_module_name(f"{subject}_Rules"),
                "purpose": f"Documented rules associated with {class_name}; grouping requires review.",
                "entity_ids": batch,
            }]}
        for module in batch_plan["modules"]:
            base = module["name"]
            candidate = base
            if candidate.casefold() in used_names or candidate.casefold() in RESERVED_STEMS:
                candidate = sanitize_module_name(f"{subject}_{base}")
            index = 2
            while candidate.casefold() in used_names or candidate.casefold() in RESERVED_STEMS:
                candidate = sanitize_module_name(f"{subject}_{base}_{index}")
                index += 1
            used_names.add(candidate.casefold())
            proposed_modules.append({**module, "name": candidate})
    tree, plan, reconciliation = _reconcile_project_modules(
        package, proposed_modules, backend, cluster_model, documentation_brief
    )
    plan["planning"] = {
        "mode": "global_capability_hierarchy",
        "discovery_batching": "ruleset_class_groups",
        "batch_count": len(batches),
        "rule_counts_by_batch": [len(batch) for batch in batches],
        "prompt_token_target": prompt_token_target,
        "fallback_batches": fallback_batches,
        "provisional_modules": proposed_modules,
        "model_calls": deepcopy(getattr(backend, "usage_events", [])[usage_start:]),
        **reconciliation,
    }
    return tree, plan


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
        def from_tree(branch: dict[str, Any]) -> list[dict[str, Any]]:
            result = []
            for name, info in branch.items():
                item = {"name": name, "purpose": rationale_map.get(name, "")}
                if isinstance(info, dict) and info.get("children"):
                    item["children"] = from_tree(info["children"])
                else:
                    item["entity_ids"] = info.get("components") if isinstance(info, dict) else None
                result.append(item)
            return result
        modules = from_tree(proposed["module_tree"])
    if not isinstance(modules, list):
        raise ValueError("Pega planner must return a JSON object with modules")
    scope_ids = documented_rule_ids(package)
    ownership: dict[str, str] = {}
    rationale: dict[str, str] = {}
    used_names: set[str] = set()
    module_paths: dict[str, list[str]] = {}

    def visit(items: list[dict[str, Any]], path: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        tree: dict[str, Any] = {}
        canonical: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Every Pega module must be an object")
            requested_name = str(item.get("name") or "")
            name = sanitize_module_name(requested_name)
            if name != requested_name or name.casefold() in used_names or name.casefold() in RESERVED_STEMS:
                raise ValueError(f"Unsafe or duplicate Pega module name: {requested_name!r}")
            used_names.add(name.casefold())
            rationale[name] = str(item.get("purpose") or "")
            module_paths[name] = [*path, name]
            children = item.get("children", [])
            if not isinstance(children, list):
                raise ValueError(f"Pega module {name} children must be a list")
            if children:
                if item.get("entity_ids"):
                    raise ValueError(f"Parent module {name} cannot own rules as well as children")
                child_tree, canonical_children = visit(children, [*path, name])
                # Parent components are a rollup for shared CodeWiki machinery;
                # primary ownership always remains at the leaves.
                ids = [entity_id for info in child_tree.values() for entity_id in info["components"]]
                canonical.append({"name": name, "purpose": rationale[name], "children": canonical_children})
            else:
                ids = item.get("entity_ids")
                if not isinstance(ids, list) or not ids:
                    raise ValueError(f"Pega leaf module {name} needs a nonempty entity_ids list")
                for entity_id in ids:
                    if not isinstance(entity_id, str) or entity_id not in scope_ids:
                        raise ValueError(f"Unknown or unsupported Pega owner ID: {entity_id!r}")
                    if entity_id in ownership:
                        raise ValueError(f"Pega rule {entity_id} has two owners")
                    ownership[entity_id] = name
                child_tree = {}
                canonical.append({"name": name, "purpose": rationale[name], "entity_ids": list(ids)})
            tree[name] = {"components": list(ids), "children": child_tree}
        return tree, canonical

    tree, canonical_modules = visit(modules, [])
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
        "module_paths": module_paths,
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
    if isinstance(proposed.get("planning"), dict):
        plan["planning"] = deepcopy(proposed["planning"])
    if proposed.get("documentation_profile") is not None:
        profile = proposed["documentation_profile"]
        if not isinstance(profile, dict) or set(profile) != {"doc_type", "instructions"}:
            raise ValueError("Pega plan documentation profile must contain doc_type and instructions")
        if not all(isinstance(value, str) for value in profile.values()):
            raise ValueError("Pega plan documentation profile values must be strings")
        plan["documentation_profile"] = deepcopy(profile)
    return tree, plan
