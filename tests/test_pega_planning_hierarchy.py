"""Regression coverage for workflow ownership across batches and nested refresh."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codewiki.cli.pega_viewer import _navigation
from codewiki.src.be.agent_factory import module_agent_spec, module_user_prompt
from codewiki.src.be.pega_documentation_generator import PegaDocumentationGenerator, _implementation_identity
from codewiki.src.be.pega_planner import plan_project_modules, validate_plan
from codewiki.src.be.pega_prompts import PEGA_PROMPT_VERSION
from codewiki.src.be.sources.pega_mcp import PegaGraphProvider


def package():
    entities = []
    for number, name in enumerate(("Submit", "Validate", "Poll"), 1):
        entity_id = f"demo::rule:{number}"
        doc_id = f"demo::document:{number}"
        properties = {
            "id": entity_id, "name": name, "rule_type": "Rule-Obj-Activity",
            "class_name": f"Demo-Class-{number}", "ruleset": f"Set{number}",
        }
        entities.append({**properties, "properties": properties, "document_ids": [doc_id]})
    return {
        "source_kind": "pega", "project_id": "demo", "snapshot_key": "before",
        "scope": {"mode": "project"}, "entities": entities,
        "relationships": [{
            "id": "demo::edge:submit-validation", "source_entity_id": entities[0]["id"],
            "target_entity_id": entities[1]["id"], "relationship_type": "CALLS",
            "properties": {"relation_kind": "RUNS_DATA_TRANSFORM"},
        }],
        "documents": {
            entity["document_ids"][0]: {"sha256": "before", "entity_ids": [entity["id"]]}
            for entity in entities
        },
    }


def proposal():
    return {"modules": [{
        "name": "Requests", "purpose": "Submission and status tracking", "children": [
            {"name": "Submit_Workflow", "purpose": "Submit and validate together", "entity_ids": ["demo::rule:1", "demo::rule:2"]},
            {"name": "Poll_Workflow", "purpose": "Status retrieval", "entity_ids": ["demo::rule:3"]},
        ],
    }], "planning": {"mode": "global_capability_hierarchy"}}


def test_global_reconciliation_moves_rules_across_discovery_batches():
    captured = package()
    provider = SimpleNamespace(cache=SimpleNamespace(get_document=lambda _: None))
    seen = []

    def complete(prompt, model=None):
        seen.append(prompt)
        if "<RULE_CATALOG>" in prompt:
            catalog = json.loads(prompt.split("<RULE_CATALOG>")[1].split("</RULE_CATALOG>")[0])
            refs = {item["name"]: ref for ref, item in catalog.items()}
            edges = json.loads(prompt.split("<RELATIONSHIPS>")[1].split("</RELATIONSHIPS>")[0])
            assert any(e["source"] == refs["Submit"] and e["target"] == refs["Validate"] for e in edges)
            return json.dumps({"modules": [{"name": "Requests", "children": [
                {"name": "Submit_Workflow", "rule_refs": [refs["Submit"], refs["Validate"]]},
                {"name": "Poll_Workflow", "rule_refs": [refs["Poll"]]},
            ]}]})
        cards = json.loads(prompt.split("<ENTITY_CARDS>\n")[1].split("\n</ENTITY_CARDS>")[0])
        return json.dumps({"modules": [{"name": cards[0]["name"], "purpose": "provisional",
                                       "entity_ids": [card["id"] for card in cards]}]})

    backend = SimpleNamespace(complete=complete)
    with patch("codewiki.src.be.pega_planner._project_batches", return_value=[[e["id"]] for e in captured["entities"]]):
        tree, plan = plan_project_modules(captured, provider, backend, None)
    assert len(seen) == 4
    assert plan["primary_owner"]["demo::rule:1"] == plan["primary_owner"]["demo::rule:2"] == "Submit_Workflow"
    assert set(tree["Requests"]["components"]) == {e["id"] for e in captured["entities"]}
    assert plan["module_paths"]["Submit_Workflow"] == ["Requests", "Submit_Workflow"]
    assert plan["planning"]["batch_count"] == 3
    assert plan["planning"]["mode"] == "global_capability_hierarchy"


def test_validated_nested_plan_round_trip_preserves_discovery_provenance():
    tree, plan = validate_plan(package(), proposal())
    again, canonical = validate_plan(package(), plan)
    assert again == tree
    assert canonical == plan
    assert set(plan["primary_owner"].values()) == {"Submit_Workflow", "Poll_Workflow"}


def test_hierarchy_rejects_duplicate_ownership_and_parent_ownership():
    invalid = proposal()
    invalid["modules"][0]["children"][1]["entity_ids"].append("demo::rule:1")
    with pytest.raises(ValueError, match="two owners"):
        validate_plan(package(), invalid)
    invalid = proposal()
    invalid["modules"][0]["entity_ids"] = ["demo::rule:1"]
    with pytest.raises(ValueError, match="Parent module"):
        validate_plan(package(), invalid)


def test_changed_nested_document_invalidates_leaf_and_ancestors_only(tmp_path):
    before = package()
    tree, plan = validate_plan(before, proposal())
    previous = tmp_path / "previous"
    old_docs = previous / "docs"
    old_docs.mkdir(parents=True)
    for name in ("Requests", "Submit_Workflow", "Poll_Workflow", "overview"):
        (old_docs / f"{name}.md").write_text(f"# {name}\n")
    for path, value in [(previous / "plan.json", plan), (previous / "evidence-package.json", before),
                        (old_docs / "module_tree.json", tree)]:
        path.write_text(json.dumps(value))
    model = {"provider": "api", "base_url": "https://example.invalid/v1", "main_model": "test",
             "cluster_model": "test", "prompt_caching": False}
    manifest = {"model": model, "prompt_version": PEGA_PROMPT_VERSION, "implementation": _implementation_identity()}
    (previous / "evidence-manifest.json").write_text(json.dumps(manifest))
    after = deepcopy(before)
    after["snapshot_key"] = "after"
    after["documents"]["demo::document:2"]["sha256"] = "changed"
    docs = tmp_path / "new" / "docs"
    docs.mkdir(parents=True)
    generator = object.__new__(PegaDocumentationGenerator)
    generator.config = SimpleNamespace(docs_dir=str(docs), provider="api", llm_base_url=model["base_url"],
                                       main_model="test", cluster_model="test", prompt_caching=False)
    generator.provider = SimpleNamespace(project="demo")
    with patch.object(PegaGraphProvider, "load_snapshot", return_value=before):
        state = generator._prepare_incremental(after, previous)
    assert state["applied"], state
    assert state["regenerated_modules"] == ["Submit_Workflow"]
    assert state["regenerated_overviews"] == ["Requests", "overview"]
    assert state["reused_modules"] == ["Poll_Workflow"]
    assert (docs / "Poll_Workflow.md").is_file()
    assert not any((docs / f"{name}.md").exists() for name in ("Requests", "Submit_Workflow", "overview"))


def test_writer_receives_leaf_paths_and_all_incident_relationships():
    tree, _ = validate_plan(package(), proposal())
    edges = {str(i): SimpleNamespace(
        id=f"demo::edge:{i}", relationship_type="CALLS", source_entity_id="demo::rule:1",
        target_entity_id="demo::rule:2", document_id="demo::document:1",
        properties={"relation_kind": "RUNS_DATA_TRANSFORM", "step_path": str(i)},
    ) for i in range(181)}
    provider = SimpleNamespace(_relationships=edges)
    prompt = module_user_prompt("Submit_Workflow", ["demo::rule:1"],
                                {"demo::rule:1": SimpleNamespace(source_code='{"id":"demo::rule:1"}')},
                                tree, source_kind="pega", pega_provider=provider)
    sent = json.loads(prompt.split("<DIRECTED_RELATIONSHIPS>")[1].split("</DIRECTED_RELATIONSHIPS>")[0])
    assert len(sent) == 181
    assert sent[-1]["relation_kind"] == "RUNS_DATA_TRANSFORM"
    assert '"path": ["Requests", "Submit_Workflow"]' in prompt
    dummy_delegation = object()
    _, tools = module_agent_spec("Submit_Workflow", source_kind="pega", complex_module=True, delegation_tool=dummy_delegation)
    assert dummy_delegation not in tools


def test_viewer_keeps_evidence_receipts_outside_documentation_hierarchy():
    tree, _ = validate_plan(package(), proposal())
    pages = {f"docs/{name}.md": {"title": name} for name in ("Requests", "Submit_Workflow", "Poll_Workflow")}
    pages["evidence/edges/edge.md"] = {"title": "Captured relationship"}
    html = _navigation(tree, pages, {})
    documentation, evidence = html.split('evidence-navigation', 1)
    assert "Submit_Workflow" in documentation and 'class="nav-module"' in documentation
    assert "Captured relationship" not in documentation
    assert "Captured relationship" in evidence and "Relationship receipts" in evidence
