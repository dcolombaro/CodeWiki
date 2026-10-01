"""PEGA style routing and incremental identity without model calls."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codewiki.cli.pega_viewer import render_pega_viewer
from codewiki.src.be.agent_factory import module_agent_spec
from codewiki.src.be.pega_documentation_generator import (
    PegaDocumentationGenerator,
    _implementation_identity,
)
from codewiki.src.be.pega_planner import plan_project_modules, validate_plan
from codewiki.src.be.pega_prompts import (
    PEGA_PROMPT_VERSION,
    pega_documentation_brief,
    pega_documentation_profile,
)
from codewiki.src.be.sources.evidence import digest


def _package():
    entities = []
    for number, name in enumerate(("Submit", "Validate"), 1):
        entity_id = f"demo::rule:{number}"
        document_id = f"demo::document:{number}"
        properties = {
            "id": entity_id,
            "name": name,
            "rule_type": "Rule-Obj-Activity",
            "class_name": "Demo-Request",
            "ruleset": "Demo",
        }
        entities.append({**properties, "properties": properties, "document_ids": [document_id]})
    return {
        "source_kind": "pega",
        "project_id": "demo",
        "snapshot_key": "same-input",
        "scope": {"mode": "project"},
        "entities": entities,
        "relationships": [],
        "documents": {
            entity["document_ids"][0]: {"sha256": "same-content", "entity_ids": [entity["id"]]}
            for entity in entities
        },
    }


class PegaDocumentationStyleTests(unittest.TestCase):
    def test_functional_inventory_is_linked_evidence_page(self):
        package = _package()
        tree, plan = validate_plan(package, {"modules": [{
            "name": "Request_Workflow", "purpose": "Submission and validation",
            "entity_ids": [entity["id"] for entity in package["entities"]],
        }]})
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            docs = run / "docs"
            evidence = run / "evidence"
            docs.mkdir()
            evidence.mkdir()
            (docs / "module_tree.json").write_text(json.dumps(tree))
            (docs / "overview.md").write_text("# Overview\n")
            (docs / "Request_Workflow.md").write_text("# Request workflow\n\nA supported outcome.\n")
            document_names = {
                entity["document_ids"][0]: f"source-{number}.md"
                for number, entity in enumerate(package["entities"], 1)
            }
            for name in document_names.values():
                (evidence / name).write_text("# Source\n")
            edge_id = "demo::relationship:1"
            package["relationships"] = [{
                "id": edge_id,
                "source_entity_id": "demo::rule:1",
                "target_entity_id": "demo::rule:2",
                "relationship_type": "CALLS_ACTIVITY",
                "properties": {"step_path": "1"},
            }]
            (evidence / "edges").mkdir()
            (evidence / "edges" / f"{digest(edge_id)[:24]}.md").write_text("# Edge\n")
            generator = object.__new__(PegaDocumentationGenerator)
            generator.config = SimpleNamespace(doc_type="functional", custom_instructions="")
            generator.provider = SimpleNamespace(cache=SimpleNamespace(
                markdown_path=lambda document_id: Path(document_names[document_id])
            ))
            generator._append_source_inventories(docs, package, plan)
            generator._check_local_links(docs)
            chapter = (docs / "Request_Workflow.md").read_text()
            inventory = (evidence / "inventories" / "Request_Workflow.md").read_text()
            self.assertIn("[Source inventory](../evidence/inventories/Request_Workflow.md)", chapter)
            self.assertNotIn("demo::rule:1", chapter)
            self.assertIn("demo::rule:1", inventory)
            self.assertIn("[official document](../source-1.md)", inventory)
            self.assertIn(f"[edge receipt](../edges/{digest(edge_id)[:24]}.md)", inventory)
            viewer = render_pega_viewer(run).read_text()
            self.assertIn("Module inventories", viewer)
            self.assertIn("evidence%2Finventories%2FRequest_Workflow.md", viewer)

    def test_functional_brief_reaches_project_planning_and_leaf_writer(self):
        package = _package()
        config = SimpleNamespace(
            doc_type="functional",
            custom_instructions="Write for business analysts.",
            max_token_per_module=100_000,
        )
        prompts = []

        def complete(prompt, model=None):
            prompts.append(prompt)
            if "<RULE_CATALOG>" in prompt:
                catalog = json.loads(prompt.split("<RULE_CATALOG>", 1)[1].split("</RULE_CATALOG>", 1)[0])
                return json.dumps({"modules": [{
                    "name": "Request_Workflow", "purpose": "Submission and validation",
                    "rule_refs": list(catalog),
                }]})
            cards = json.loads(prompt.split("<ENTITY_CARDS>\n", 1)[1].split("\n</ENTITY_CARDS>", 1)[0])
            return json.dumps({"modules": [{
                "name": "Request_Workflow", "purpose": "Submission and validation",
                "entity_ids": [card["id"] for card in cards],
            }]})

        backend = SimpleNamespace(_config=config, complete=complete)
        provider = SimpleNamespace(cache=SimpleNamespace(get_document=lambda _id: None))
        tree, plan = plan_project_modules(package, provider, backend, None)
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all(prompt.startswith("<DOCUMENTATION_BRIEF>") for prompt in prompts))
        self.assertTrue(all("business analysts" in prompt for prompt in prompts))
        self.assertEqual(set(plan["primary_owner"]), {entity["id"] for entity in package["entities"]})
        self.assertIn("Request_Workflow", tree)

        prompt, _tools = module_agent_spec(
            "Request_Workflow", source_kind="pega", complex_module=False,
            custom_instructions=pega_documentation_brief(config), doc_type="functional",
        )
        self.assertIn("evidence-supported functional role", prompt)
        self.assertIn("business analysts", prompt)
        self.assertIn("only when it clarifies", prompt)
        self.assertIn("configured behavior in business terms", prompt)
        self.assertIn("label any inferred business implication", prompt)
        self.assertNotIn("Use exact Pega technical names and graph IDs", prompt)

    def test_functional_brief_reaches_parent_overview(self):
        with tempfile.TemporaryDirectory() as directory:
            docs = Path(directory)
            (docs / "module_tree.json").write_text(json.dumps({
                "Requests": {"components": ["demo::rule:1"], "children": {
                    "Request_Workflow": {"components": ["demo::rule:1"], "children": {}}
                }}
            }))
            (docs / "Request_Workflow.md").write_text("# Request workflow\nA supported outcome.\n")
            prompts = []
            generator = object.__new__(PegaDocumentationGenerator)
            generator.config = SimpleNamespace(doc_type="functional", custom_instructions="Business readers")
            generator.provider = SimpleNamespace(project="demo")
            generator.backend = SimpleNamespace(complete=lambda prompt: (
                prompts.append(prompt) or "<OVERVIEW># Requests\nA supported outcome.</OVERVIEW>"
            ))
            asyncio.run(generator.generate_parent_module_docs(["Requests"], str(docs)))
            self.assertIn("<DOCUMENTATION_BRIEF>", prompts[0])
            self.assertIn("Business readers", prompts[0])
            self.assertTrue((docs / "Requests.md").is_file())

    def test_style_change_rejects_incremental_page_reuse(self):
        package = _package()
        tree, plan = validate_plan(package, {"modules": [{
            "name": "Request_Workflow", "purpose": "Submission and validation",
            "entity_ids": [entity["id"] for entity in package["entities"]],
        }]})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = root / "previous"
            previous_docs = previous / "docs"
            previous_docs.mkdir(parents=True)
            (previous_docs / "module_tree.json").write_text(json.dumps(tree))
            for name in ("Request_Workflow", "overview"):
                (previous_docs / f"{name}.md").write_text(f"# {name}\n")
            (previous / "plan.json").write_text(json.dumps(plan))
            (previous / "evidence-package.json").write_text(json.dumps(package))
            model = {
                "provider": "openai-compatible", "base_url": "https://example.invalid/v1",
                "main_model": "example", "cluster_model": "example", "prompt_caching": False,
            }
            (previous / "evidence-manifest.json").write_text(json.dumps({
                "initial_snapshot_key": package["snapshot_key"],
                "prompt_version": PEGA_PROMPT_VERSION,
                "implementation": _implementation_identity(),
                "model": model,
                "documentation_profile": {"doc_type": "default", "instructions": ""},
            }))
            docs = root / "current" / "docs"
            docs.mkdir(parents=True)
            generator = object.__new__(PegaDocumentationGenerator)
            generator.config = SimpleNamespace(
                docs_dir=str(docs), provider=model["provider"],
                llm_base_url=model["base_url"], main_model=model["main_model"],
                cluster_model=model["cluster_model"], prompt_caching=False,
                doc_type="functional", custom_instructions="",
            )
            generator.provider = SimpleNamespace(project="demo")
            with patch("codewiki.src.be.sources.pega_mcp.PegaGraphProvider.load_snapshot", return_value=package):
                state = generator._prepare_incremental(package, previous)
            self.assertFalse(state["applied"])
            self.assertIn("documentation_profile_changed", state["fallback_reasons"])
            self.assertFalse((docs / "Request_Workflow.md").exists())
            self.assertEqual(pega_documentation_profile(generator.config)["doc_type"], "functional")


if __name__ == "__main__":
    unittest.main()
