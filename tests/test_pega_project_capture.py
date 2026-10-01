"""Project-wide PEGA capture and planning must prove complete rule coverage."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from codewiki.src.be.pega_planner import documented_rule_ids, plan_project_modules
from codewiki.src.be.sources.evidence import PegaEntity
from codewiki.src.be.sources.pega_mcp import PegaGraphProvider


class ProjectTransport:
    def __init__(self, *, entity_count: int = 501, reported_entities: int | None = None) -> None:
        self.entities = [
            {
                "id": f"demo::pega:{index:04d}",
                "name": f"Rule {index}",
                "entity_type": "PegaActivity",
                "rule_type": "Rule-Obj-Activity" if index == 0 else "",
                "class_name": "Demo-Work",
                "ruleset": "Demo",
                "project_id": "demo",
                "projection_token": "revision-1",
            }
            for index in range(entity_count)
        ]
        self.reported_entities = reported_entities if reported_entities is not None else entity_count
        self.edges = [
            {
                "source_entity_id": self.entities[0]["id"],
                "target_entity_id": self.entities[-1]["id"],
                "relationship_type": "CALLS_ACTIVITY",
                "evidence": {
                    "id": "demo::relationship:one",
                    "project_id": "demo",
                    "projection_token": "revision-1",
                    "document_id": "demo::document:one",
                },
            }
        ]

    async def call(self, name: str, arguments: dict) -> dict:
        if name == "list_projects":
            return {"projects": [{"project_id": "demo"}]}
        if name == "kb_status":
            return {
                "project_id": "demo", "entities": self.reported_entities,
                "documents": 1, "relationships": 2,
            }
        if name == "describe_graph":
            return {"relationship_shapes": [{"relationship_type": "CALLS_ACTIVITY"}]}
        if name == "search_entities":
            offset = arguments["offset"]
            limit = arguments["limit"]
            page = self.entities[offset : offset + limit]
            return {
                "results": [
                    {
                        "entity": entity,
                        "source_documents": (
                            [{"document_id": "demo::document:one"}] if entity is self.entities[0] else []
                        ),
                    }
                    for entity in page
                ],
                "has_more": offset + limit < len(self.entities),
                "next_offset": offset + len(page) if offset + limit < len(self.entities) else None,
            }
        if name == "read_cypher":
            if "count(edge)" in arguments["statement"]:
                return {"rows": [{"relationship_count": len(self.edges)}]}
            offset = arguments["parameters"]["offset"]
            limit = arguments["parameters"]["limit"]
            return {"rows": self.edges[offset : offset + limit]}
        if name == "get_document":
            return {
                "document": {"id": "demo::document:one", "title": "Rule 0", "source_path": "rules/one.md"},
                "markdown": "# Rule 0\n\n## Extracted configuration\n\nA configured call.\n",
                "entities": [{"id": self.entities[0]["id"]}],
            }
        raise AssertionError(name)


class ProjectCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_capture_includes_disconnected_entities_and_paged_inventory(self):
        with TemporaryDirectory() as temporary:
            source_root = Path(temporary)
            (source_root / "rules").mkdir()
            (source_root / "rules/one.md").write_text(
                "# Rule 0\n\n## Extracted configuration\n\nA configured call.\n",
                encoding="utf-8",
            )
            transport = ProjectTransport()
            provider = PegaGraphProvider(
                transport,
                project="demo",
                cache_dir=Path(temporary) / "evidence",
                source_root=source_root,
            )
            await provider.initialize()
            package = await provider.snapshot_project()

        self.assertEqual(package["scope"]["mode"], "project")
        self.assertEqual(package["scope"]["entity_count"], 501)
        self.assertEqual(package["scope"]["relationship_count"], 1)
        self.assertEqual(package["scope"]["document_count"], 1)
        self.assertEqual(package["relationships"][0]["source_entity_id"], "demo::pega:0000")
        self.assertEqual(package["relationships"][0]["target_entity_id"], "demo::pega:0500")
        self.assertTrue(package["documents"]["demo::document:one"]["sha256"])
        self.assertEqual(sum(request["tool"] == "search_entities" for request in package["requests"]), 2)

    async def test_project_capture_rejects_incomplete_inventory(self):
        with TemporaryDirectory() as temporary:
            provider = PegaGraphProvider(
                ProjectTransport(entity_count=2, reported_entities=3),
                project="demo", cache_dir=Path(temporary) / "evidence",
            )
            await provider.initialize()
            with self.assertRaisesRegex(RuntimeError, "entity inventory is incomplete"):
                await provider.snapshot_project()


class ProjectPlannerTests(unittest.TestCase):
    def test_project_planner_assigns_every_rule_without_a_fixed_rule_count_cap(self):
        class Cache:
            def get_document(self, _document_id):
                return None

        class Provider:
            cache = Cache()

        class Backend:
            def __init__(self):
                self.batch_sizes = []
                self.global_calls = 0

            def complete(self, prompt, model=None):
                if "<RULE_CATALOG>" in prompt:
                    self.global_calls += 1
                    rules = json.loads(prompt.split("<RULE_CATALOG>", 1)[1].split("</RULE_CATALOG>", 1)[0])
                    return json.dumps({"modules": [{
                        "name": "Business_Rules", "purpose": "One coherent capability across discovery groups",
                        "rule_refs": list(rules),
                    }]})
                cards = json.loads(prompt.split("<ENTITY_CARDS>\n", 1)[1].split("\n</ENTITY_CARDS>", 1)[0])
                self.batch_sizes.append(len(cards))
                return json.dumps({"modules": [{
                    "name": "Business_Rules",
                    "purpose": "Rules in this bounded group",
                    "entity_ids": [card["id"] for card in cards],
                }]})

        entities = [
            {
                "id": f"demo::pega:{index:04d}", "name": f"Rule {index}",
                "rule_type": "Rule-Obj-Activity", "rule_category": "activity",
                "entity_type": "PegaActivity", "class_name": f"Demo-Work-{index // 10}",
                "ruleset": "Demo", "is_external": False, "is_embedded": False,
                "document_ids": [f"demo::document:{index:04d}"],
            }
            for index in range(60)
        ]
        package = {
            "project_id": "demo", "snapshot_key": "snapshot", "scope": {"mode": "project"},
            "entities": entities, "relationships": [],
            "documents": {entity["document_ids"][0]: {} for entity in entities},
        }
        backend = Backend()
        tree, plan = plan_project_modules(package, Provider(), backend, None)

        self.assertEqual(len(plan["primary_owner"]), 60)
        self.assertEqual(len(tree), 1)
        self.assertEqual(backend.global_calls, 1)
        self.assertTrue(all(size > 0 for size in backend.batch_sizes))
        self.assertEqual(sum(backend.batch_sizes), 60)
        self.assertEqual(plan["planning"]["batch_count"], len(backend.batch_sizes))


class ProjectEligibilityTests(unittest.TestCase):
    def test_official_document_and_rule_type_define_page_eligibility(self):
        project = "demo"
        external_id = f"{project}::pega:external"
        embedded_id = f"{project}::pega:embedded"
        undocumented_id = f"{project}::pega:undocumented"
        context_id = f"{project}::pega:context"
        entities = [
            {
                "id": external_id, "name": "ExternalRule", "entity_type": "PegaActivity",
                "rule_type": "Rule-Obj-Activity", "is_external": True,
                "document_ids": [f"{project}::document:external"],
            },
            {
                "id": embedded_id, "name": "EmbeddedRule", "entity_type": "PegaActivity",
                "rule_type": "Rule-Obj-Activity", "is_embedded": True,
                "document_ids": [f"{project}::document:embedded"],
            },
            {
                "id": undocumented_id, "name": "UndocumentedRule", "entity_type": "PegaActivity",
                "rule_type": "Rule-Obj-Activity", "document_ids": [f"{project}::document:missing"],
            },
            {
                "id": context_id, "name": "Context", "entity_type": "PegaClass",
                "document_ids": [f"{project}::document:context"],
            },
        ]
        package = {
            "project_id": project,
            "snapshot_key": "test",
            "entities": entities,
            "relationships": [],
            "documents": {
                f"{project}::document:external": {},
                f"{project}::document:embedded": {},
                f"{project}::document:context": {},
            },
        }

        with TemporaryDirectory() as temporary:
            provider = PegaGraphProvider(
                None, project=project, cache_dir=Path(temporary) / "evidence"
            )
            components = provider.codewiki_components(package)

        expected = {external_id, embedded_id}
        self.assertEqual(documented_rule_ids(package), expected)
        self.assertEqual(set(components), expected)
        self.assertTrue(
            PegaEntity.from_result(entities[0], [f"{project}::document:external"]).documented_rule
        )
        self.assertTrue(
            PegaEntity.from_result(entities[1], [f"{project}::document:embedded"]).documented_rule
        )
