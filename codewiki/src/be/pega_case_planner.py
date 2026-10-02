"""Case Type anchored planning for focused PEGA process documentation."""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from typing import Any

from codewiki.src.be.module_naming import RESERVED_STEMS, sanitize_module_name
from codewiki.src.be.pega_planner import documented_rule_ids, plan_project_modules, validate_plan
from codewiki.src.be.sources.pega_mcp import PegaGraphProvider


def _configuration_array(markdown: str, heading: str) -> list[dict[str, Any]]:
    match = re.search(
        rf"(?m)^### {re.escape(heading)}[ \t]*\n+[ \t]*```json[ \t]*\n(.*?)\n```",
        markdown,
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError(f"Case Type Markdown lacks the {heading} configuration array")
    rows = json.loads(match.group(1))
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"Case Type {heading} configuration must be an array of objects")
    return rows


def _case_spec(
    package: dict[str, Any], provider: PegaGraphProvider, case_id: str | None = None,
) -> dict[str, Any] | None:
    """Read one Case Type's ordered stages from its official Markdown."""
    seeds = [case_id] if case_id is not None else (package.get("scope") or {}).get("seed_entity_ids") or []
    entities = {row["id"]: row for row in package.get("entities") or []}
    if len(seeds) != 1 or entities.get(seeds[0], {}).get("rule_type") != "Rule-Obj-CaseType":
        return None
    case = entities[seeds[0]]
    documents = case.get("document_ids") or []
    if len(documents) != 1:
        raise ValueError("Focused Case Type must have exactly one official Markdown document")
    document = provider.cache.get_document(documents[0])
    if document is None:
        raise ValueError("The selected Case Type Markdown is not cached")
    markdown = document.markdown
    label_match = re.search(r"(?m)^\| `pyLabel` \| (.*?) \|[ \t]*$", markdown)
    label = label_match.group(1).strip() if label_match else str(case.get("class_name") or case["name"])
    stages = _configuration_array(markdown, "stages")
    processes = _configuration_array(markdown, "processes")
    if not stages or not processes:
        raise ValueError("The selected Case Type has no captured stages or processes")
    flow_edges = {
        str((edge.get("properties") or {}).get("call_site_id")): edge
        for edge in package.get("relationships") or []
        if edge["source_entity_id"] == case["id"]
        and (edge.get("properties") or {}).get("relation_kind") == "STARTS_FLOW"
    }
    ordered = []
    for stage in stages:
        source_name = str(stage.get("pyStageName") or "").strip()
        if not source_name:
            continue
        entries = []
        for process in processes:
            if process.get("stage") != source_name:
                continue
            call_site = f"{process.get('step_path')}:flow"
            edge = flow_edges.get(call_site)
            if edge is None:
                raise ValueError(f"Case process {source_name}/{process.get('pyFlowName')} has no captured STARTS_FLOW edge")
            target = entities.get(edge["target_entity_id"])
            if target is None or target.get("rule_type") != "Rule-Obj-Flow":
                raise ValueError(f"Case process {source_name} does not resolve to a captured Flow rule")
            entries.append({"flow_id": target["id"], "flow_name": target["name"], "step_path": process.get("step_path")})
        if entries:
            readable_name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", source_name).replace("_", " ")
            ordered.append({
                "source_name": source_name,
                "label": "Initialization" if source_name.casefold() == "initalize" else readable_name,
                "kind": str(stage.get("stage_kind") or "PRIMARY"),
                "step_path": stage.get("step_path"),
                "flows": entries,
            })
    if not ordered:
        raise ValueError("The focused Case Type has no captured stage Flow rules")
    return {"case_id": case["id"], "document_id": documents[0], "label": label, "stages": ordered}


def _stage_distances(
    package: dict[str, Any], spec: dict[str, Any],
    *, blocked_case_ids: set[str] | None = None, blocked_flow_ids: set[str] | None = None,
) -> dict[int, dict[str, int]]:
    """Trace directed dependencies while keeping each stage's Flow as its anchor."""
    adjacent: dict[str, set[str]] = defaultdict(set)
    for edge in package.get("relationships") or []:
        if edge["relationship_type"] in {"CALLS", "READS", "WRITES"}:
            adjacent[edge["source_entity_id"]].add(edge["target_entity_id"])
    flow_roots = (
        {flow["flow_id"] for stage in spec["stages"] for flow in stage["flows"]}
        | (blocked_flow_ids or set())
    )
    blocked_cases = blocked_case_ids or {spec["case_id"]}
    result: dict[int, dict[str, int]] = {}
    for index, stage in enumerate(spec["stages"]):
        own_roots = {flow["flow_id"] for flow in stage["flows"]}
        distances = {entity_id: 0 for entity_id in own_roots}
        queue = deque(own_roots)
        while queue:
            current = queue.popleft()
            for target in sorted(adjacent[current]):
                if target in blocked_cases or target in flow_roots - own_roots or target in distances:
                    continue
                distances[target] = distances[current] + 1
                queue.append(target)
        result[index] = distances
    return result


def _case_lifecycle_distances(
    package: dict[str, Any], case_id: str, blocked_case_ids: set[str],
    blocked_flow_ids: set[str],
) -> dict[str, int]:
    """Trace case-wide actions without crossing into any stage Flow."""
    adjacent: dict[str, set[str]] = defaultdict(set)
    for edge in package.get("relationships") or []:
        if edge["relationship_type"] in {"CALLS", "READS", "WRITES"}:
            adjacent[edge["source_entity_id"]].add(edge["target_entity_id"])
    distances = {case_id: 0}
    queue = deque([case_id])
    while queue:
        current = queue.popleft()
        for target in sorted(adjacent[current]):
            if target in blocked_flow_ids or target in blocked_case_ids - {case_id} or target in distances:
                continue
            distances[target] = distances[current] + 1
            queue.append(target)
    return distances


def _flatten_leaves(modules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    leaves = []
    for module in modules:
        if module.get("children"):
            leaves.extend(_flatten_leaves(module["children"]))
        else:
            leaves.append(module)
    return leaves


def plan_case_modules(
    package: dict[str, Any], provider: PegaGraphProvider, backend: Any, cluster_model: str | None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Anchor a focused wiki to its Case Type, then plan within fixed stages."""
    spec = _case_spec(package, provider)
    if spec is None:
        return None
    owners = documented_rule_ids(package)
    distances = _stage_distances(package, spec)
    stage_ids: dict[int, set[str]] = {index: set() for index in distances}
    shared: set[str] = set()
    lifecycle = {spec["case_id"]}
    flow_stage = {
        flow["flow_id"]: index
        for index, stage in enumerate(spec["stages"])
        for flow in stage["flows"]
    }
    for entity_id in owners - lifecycle:
        if entity_id in flow_stage:
            stage_ids[flow_stage[entity_id]].add(entity_id)
            continue
        reachable = {index: d[entity_id] for index, d in distances.items() if entity_id in d}
        if not reachable:
            lifecycle.add(entity_id)
            continue
        minimum = min(reachable.values())
        nearest = [index for index, distance in reachable.items() if distance == minimum]
        if len(nearest) == 1:
            stage_ids[nearest[0]].add(entity_id)
        else:
            shared.add(entity_id)

    used_names: set[str] = set(RESERVED_STEMS)

    def name(base: str) -> str:
        stem = sanitize_module_name(base)
        candidate = stem
        number = 2
        while candidate.casefold() in used_names:
            candidate = f"{stem}_{number}"
            number += 1
        used_names.add(candidate.casefold())
        return candidate

    def planned_leaves(ids: set[str], prefix: str) -> list[dict[str, Any]]:
        if not ids:
            return []
        if len(ids) <= 25:
            return [{"name": name(prefix), "purpose": f"Shared functional behavior for {prefix.replace('_', ' ')}.", "entity_ids": sorted(ids)}]
        partial = {
            **package,
            "scope": {**package["scope"], "mode": "project"},
            "entities": [entity for entity in package["entities"] if entity["id"] in ids],
            "relationships": [edge for edge in package["relationships"] if edge["source_entity_id"] in ids and edge["target_entity_id"] in ids],
        }
        # Case Type stages can contain a large ruleset/class group. Divide
        # those discovery prompts between whole rules; the reconciliation pass
        # still sees and can regroup every rule in the stage.
        _, subplan = plan_project_modules(
            partial, provider, backend, cluster_model,
            prompt_token_target=min(
                int(getattr(getattr(backend, "_config", None), "max_token_per_module", 36_369)),
                16_000,
            ),
            split_oversized_groups=True,
        )
        return [
            {"name": name(f"{prefix}_{leaf['name']}"), "purpose": leaf.get("purpose") or "Related process behavior.", "entity_ids": leaf["entity_ids"]}
            for leaf in _flatten_leaves(subplan["modules"])
        ]

    stem = sanitize_module_name(spec["label"])
    parent_name = name(f"{stem}_process")
    children = [{
        "name": name(f"{stem}_lifecycle"),
        "purpose": f"Explain the {spec['label']} business process, ordered stages, entry and case-wide actions from the Case Type.",
        "entity_ids": sorted(lifecycle),
    }]
    for index, stage in enumerate(spec["stages"]):
        stage_name = f"{stem}_{sanitize_module_name(stage['label'])}"
        ids = stage_ids[index]
        flows = {flow["flow_id"] for flow in stage["flows"]}
        purpose = f"Describe the {stage['label']} stage of {spec['label']} from its Flow and supporting rules. Source stage: {stage['source_name']}."
        if len(ids) <= 25:
            children.append({"name": name(stage_name), "purpose": purpose, "entity_ids": sorted(ids)})
        else:
            stage_children = [{"name": name(f"{stage_name}_journey"), "purpose": purpose, "entity_ids": sorted(flows)}]
            stage_children.extend(planned_leaves(ids - flows, stage_name))
            children.append({"name": name(stage_name), "purpose": purpose, "children": stage_children})
    if shared:
        shared_children = planned_leaves(shared, f"{stem}_shared")
        if len(shared_children) == 1:
            children.extend(shared_children)
        else:
            children.append({
                "name": name(f"{stem}_shared_capabilities"),
                "purpose": f"Decisions, data and integrations reused across {spec['label']} stages.",
                "children": shared_children,
            })
    primary_stages = [stage["label"] for stage in spec["stages"] if stage["kind"] != "ALTERNATE"]
    proposal = {
        "modules": [{
            "name": parent_name,
            "purpose": (
                f"End-to-end {spec['label']} process through its configured stages: "
                f"{', '.join(primary_stages or [stage['label'] for stage in spec['stages']])}."
            ),
            "children": children,
        }],
        "planning": {
            "mode": "case_type_stage_hierarchy",
            "case_type_id": spec["case_id"],
            "case_type_document_id": spec["document_id"],
            "stage_order": [{"name": stage["source_name"], "kind": stage["kind"], "flow_ids": [flow["flow_id"] for flow in stage["flows"]]} for stage in spec["stages"]],
            "shared_rule_count": len(shared),
            "lifecycle_rule_count": len(lifecycle),
        },
    }
    return validate_plan(package, proposal)


def plan_case_type_project_modules(
    package: dict[str, Any], provider: PegaGraphProvider, backend: Any, cluster_model: str | None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Anchor a functional whole-project wiki to every documented Case Type.

    Case Types and Flow rules define the fixed process hierarchy. The model
    groups rules only within those process boundaries and in the residual
    non-case capability area.
    """
    owners = documented_rule_ids(package)
    entities = {entity["id"]: entity for entity in package.get("entities") or []}
    case_ids = sorted(
        entity_id for entity_id in owners
        if entities[entity_id].get("rule_type") == "Rule-Obj-CaseType"
    )
    if not case_ids:
        return None
    specs = [_case_spec(package, provider, case_id) for case_id in case_ids]
    if any(spec is None for spec in specs):
        raise RuntimeError("A documented Case Type could not be read from the project evidence")
    case_specs = [spec for spec in specs if spec is not None]
    all_flow_ids = {
        flow["flow_id"] for spec in case_specs
        for stage in spec["stages"] for flow in stage["flows"]
    }
    missing_flow_docs = sorted(all_flow_ids - owners)
    if missing_flow_docs:
        raise ValueError(
            "Project Case Type hierarchy requires official Markdown for its stage Flows: "
            + ", ".join(missing_flow_docs)
        )
    all_case_ids = set(case_ids)
    stage_maps = [
        _stage_distances(
            package, spec, blocked_case_ids=all_case_ids,
            blocked_flow_ids=all_flow_ids,
        )
        for spec in case_specs
    ]
    lifecycle_maps = [
        _case_lifecycle_distances(package, spec["case_id"], all_case_ids, all_flow_ids)
        for spec in case_specs
    ]
    assigned: dict[str, set[str]] = {
        spec["case_id"]: {spec["case_id"]} | {
            flow["flow_id"] for stage in spec["stages"] for flow in stage["flows"]
        }
        for spec in case_specs
    }
    forced = set().union(*assigned.values())
    if sum(map(len, assigned.values())) != len(forced):
        raise ValueError("A stage Flow belongs to more than one Case Type")
    cross_case_shared: set[str] = set()
    other_capabilities: set[str] = set()
    for entity_id in sorted(owners - forced):
        reachable: dict[str, int] = {}
        for spec, stages, lifecycle in zip(case_specs, stage_maps, lifecycle_maps):
            # Stage Flows are one graph hop from their Case Type. Compare
            # downstream distances with case-wide action distances fairly.
            distances = [
                distance + 1 for stage in stages.values()
                if (distance := stage.get(entity_id)) is not None
            ]
            if entity_id in lifecycle:
                distances.append(lifecycle[entity_id])
            if distances:
                reachable[spec["case_id"]] = min(distances)
        if not reachable:
            other_capabilities.add(entity_id)
            continue
        nearest_distance = min(reachable.values())
        nearest_cases = [case_id for case_id, distance in reachable.items() if distance == nearest_distance]
        if len(nearest_cases) == 1:
            assigned[nearest_cases[0]].add(entity_id)
        else:
            cross_case_shared.add(entity_id)

    used_names: set[str] = set(RESERVED_STEMS)

    def unique_name(base: str) -> str:
        stem = sanitize_module_name(base)
        candidate = stem
        number = 2
        while candidate.casefold() in used_names:
            candidate = f"{stem}_{number}"
            number += 1
        used_names.add(candidate.casefold())
        return candidate

    def rename_modules(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        renamed = []
        for item in items:
            record = {"name": unique_name(item["name"]), "purpose": item.get("purpose") or ""}
            if item.get("children"):
                record["children"] = rename_modules(item["children"])
            else:
                record["entity_ids"] = item["entity_ids"]
            renamed.append(record)
        return renamed

    def capability_branch(ids: set[str], name: str, purpose: str) -> dict[str, Any] | None:
        if not ids:
            return None
        if len(ids) <= 25:
            return {"name": unique_name(name), "purpose": purpose, "entity_ids": sorted(ids)}
        partial = {
            **package,
            "entities": [entity for entity in package["entities"] if entity["id"] in ids],
            "relationships": [
                edge for edge in package["relationships"]
                if edge["source_entity_id"] in ids and edge["target_entity_id"] in ids
            ],
        }
        _, subplan = plan_project_modules(partial, provider, backend, cluster_model)
        return {
            "name": unique_name(name), "purpose": purpose,
            "children": rename_modules(subplan["modules"]),
        }

    modules = []
    for case_index, spec in enumerate(case_specs):
        selected = assigned[spec["case_id"]]
        context_ids = (
            set().union(*(set(stage) for stage in stage_maps[case_index].values()))
            | set(lifecycle_maps[case_index])
        ) - selected
        selected_with_context = selected | context_ids
        partial = {
            **package,
            "scope": {**package["scope"], "mode": "focused", "seed_entity_ids": [spec["case_id"]]},
            "entities": [
                {**entity, "document_ids": []} if entity["id"] in context_ids else entity
                for entity in package["entities"] if entity["id"] in selected_with_context
            ],
            "relationships": [
                edge for edge in package["relationships"]
                if edge["source_entity_id"] in selected_with_context
                and edge["target_entity_id"] in selected_with_context
            ],
        }
        planned = plan_case_modules(partial, provider, backend, cluster_model)
        if planned is None:
            raise RuntimeError(f"Could not plan documented Case Type {spec['case_id']}")
        modules.extend(rename_modules(planned[1]["modules"]))
    for ids, name, purpose in (
        (cross_case_shared, "shared_across_case_types", "Dependencies reused by multiple Case Types."),
        (other_capabilities, "other_project_capabilities", "Documented rules outside the captured Case Type journeys."),
    ):
        branch = capability_branch(ids, name, purpose)
        if branch is not None:
            modules.append(branch)
    proposal = {
        "modules": modules,
        "planning": {
            "mode": "case_type_project_hierarchy",
            "case_types": [
                {"case_type_id": spec["case_id"], "label": spec["label"],
                 "assigned_rule_count": len(assigned[spec["case_id"]]),
                 "stage_order": [stage["source_name"] for stage in spec["stages"]]}
                for spec in case_specs
            ],
            "cross_case_shared_rule_count": len(cross_case_shared),
            "other_capability_rule_count": len(other_capabilities),
        },
    }
    return validate_plan(package, proposal)
