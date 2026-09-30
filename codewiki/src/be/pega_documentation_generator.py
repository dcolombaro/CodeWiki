"""Pega source mode for CodeWiki's module writer and overview workflow."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from codewiki.src.be.documentation_generator import DocumentationGenerator, IncompleteDocumentationError
from codewiki.src.be.pydantic_ai_backend import PydanticAIBackend
from codewiki.src.be.pega_planner import documented_rule_ids, plan_modules, plan_project_modules, validate_plan
from codewiki.src.be.pega_prompts import PEGA_OVERVIEW_PROMPT, PEGA_PROMPT_VERSION
from codewiki.src.be.sources.evidence import digest, stable_json
from codewiki.src.be.sources.pega_diff import compare_evidence_packages
from codewiki.src.be.sources.pega_mcp import PegaGraphProvider, save_evidence_package
from codewiki.src.config import FIRST_MODULE_TREE_FILENAME, MODULE_TREE_FILENAME, OVERVIEW_FILENAME, Config
from codewiki.src.utils import file_manager


def _implementation_identity() -> dict[str, Any]:
    """Record this fork's source identity without placing credentials in output."""
    root = Path(__file__).resolve().parents[3]
    paths = (
        "codewiki/cli/commands/pega.py",
        "codewiki/src/be/agent_factory.py",
        "codewiki/src/be/agent_tools/deps.py",
        "codewiki/src/be/agent_tools/generate_sub_module_documentations.py",
        "codewiki/src/be/agent_tools/pega.py",
        "codewiki/src/be/agent_tools/str_replace_editor.py",
        "codewiki/src/be/documentation_generator.py",
        "codewiki/src/be/llm_services.py",
        "codewiki/src/be/pega_documentation_generator.py",
        "codewiki/src/be/pega_planner.py",
        "codewiki/src/be/pega_prompts.py",
        "codewiki/src/be/pydantic_ai_backend.py",
        "codewiki/src/be/sources/evidence.py",
        "codewiki/src/be/sources/pega_diff.py",
        "codewiki/src/be/sources/pega_mcp.py",
        "codewiki/src/be/utils.py",
        "codewiki/src/config.py",
        "scripts/pega_wiki_demo.sh",
    )
    hashes = {
        relative: digest((root / relative).read_text(encoding="utf-8"))
        for relative in paths
        if (root / relative).is_file()
    }
    try:
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        base_commit = revision.stdout.strip() if revision.returncode == 0 else None
    except OSError:
        base_commit = None
    return {
        "base_commit": base_commit,
        "source_hashes": hashes,
    }


class PegaDocumentationGenerator(DocumentationGenerator):
    """Use a frozen MCP evidence package as CodeWiki's documentation source."""

    def __init__(self, config: Config, provider: PegaGraphProvider) -> None:
        if config.source_kind != "pega":
            raise ValueError("PegaDocumentationGenerator requires source_kind='pega'")
        super().__init__(config)
        if not isinstance(self.backend, PydanticAIBackend):
            raise ValueError("Pega generation requires the API-key PydanticAI backend")
        self.provider = provider
        self.backend.pega_provider = provider

    def _write_edge_receipts(self, edges: list[dict[str, Any]], evidence_dir: Path) -> None:
        edge_dir = evidence_dir / "edges"
        edge_dir.mkdir(exist_ok=True)
        for edge in edges:
            path = edge_dir / f"{digest(edge['id'])[:24]}.md"
            document_id = edge.get("document_id") or ""
            document = self.provider.cache.get_document(document_id)
            if document is not None:
                try:
                    first_line, _ = document.section_bounds("Extracted relationships")
                    source_section = (
                        f"- Official document section: "
                        f"[Extracted relationships](../{self.provider.cache.markdown_path(document_id).name}"
                        f"#extracted-relationships), starting at line {first_line}\n"
                    )
                except KeyError:
                    source_section = "- Official document section: unavailable\n"
            else:
                source_section = "- Official document section: unavailable\n"
            qualifiers = "".join(
                f"- {label}: `{edge['properties'][key]}`\n"
                for key, label in (
                    ("http_method", "HTTP method"),
                    ("step_path", "Activity step"),
                    ("condition", "Condition"),
                    ("resolution_outcome", "Resolution outcome"),
                    ("source_location", "Source locator"),
                )
                if edge["properties"].get(key) not in (None, "")
            )
            path.write_text(
                f"# Pega graph relationship {edge['id']}\n\n"
                f"- Source: `{edge['source_entity_id']}`\n"
                f"- Type: `{edge['relationship_type']}`\n"
                f"- Target: `{edge['target_entity_id']}`\n"
                f"- Supporting document: `{document_id}`\n"
                + source_section
                + qualifiers
                + "\n"
                "## Original MCP relationship properties\n\n```json\n"
                + json.dumps(edge["properties"], ensure_ascii=False, indent=2, sort_keys=True)
                + "\n```\n",
                encoding="utf-8",
            )

    @staticmethod
    def _tree_page_names(tree: dict[str, Any]) -> set[str]:
        names: set[str] = set()

        def visit(branch: dict[str, Any]) -> None:
            for name, info in branch.items():
                names.add(name)
                visit(info.get("children") or {})

        visit(tree)
        return names

    @staticmethod
    def _subtree_names(tree: dict[str, Any], root: str) -> set[str]:
        info = tree[root]
        names = {root}
        for child in (info.get("children") or {}):
            names.update(PegaDocumentationGenerator._subtree_names(info["children"], child))
        return names

    def _prepare_incremental(
        self, package: dict[str, Any], previous_run: Path | None
    ) -> dict[str, Any]:
        """Reuse unchanged pages from a prior wiki when source and writer identity match."""
        state: dict[str, Any] = {
            "enabled": previous_run is not None,
            "applied": False,
            "source_run": str(previous_run) if previous_run else None,
            "regenerated_modules": [],
            "reused_modules": [],
        }
        if previous_run is None:
            return state

        previous_run = previous_run.resolve()
        previous_docs = previous_run / "docs"
        previous_package_path = previous_run / "evidence-package.json"
        previous_plan_path = previous_run / "plan.json"
        previous_manifest_path = previous_run / "evidence-manifest.json"
        required = (
            previous_docs / "module_tree.json",
            previous_package_path,
            previous_plan_path,
            previous_manifest_path,
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            state["reason"] = "previous_run_missing_artifacts"
            state["details"] = missing
            return state

        try:
            previous_plan = json.loads(previous_plan_path.read_text(encoding="utf-8"))
            previous_tree = json.loads((previous_docs / "module_tree.json").read_text(encoding="utf-8"))
            previous_manifest = json.loads(previous_manifest_path.read_text(encoding="utf-8"))
            current_model = {
                "provider": self.config.provider,
                "base_url": self.config.llm_base_url,
                "main_model": self.config.main_model,
                "cluster_model": self.config.cluster_model,
                "prompt_caching": self.config.prompt_caching,
            }
            artifacts = previous_manifest.get("generation_artifacts") or {}
            exact_snapshot = (
                previous_manifest.get("initial_snapshot_key") == package.get("snapshot_key")
                and previous_plan.get("snapshot_key") == package.get("snapshot_key")
            )
            exact_generation = (
                previous_manifest.get("prompt_version") == PEGA_PROMPT_VERSION
                and (previous_manifest.get("implementation") or {}).get("source_hashes")
                == _implementation_identity().get("source_hashes")
                and previous_manifest.get("model") == current_model
            )
            artifact_hashes_match = (
                artifacts.get("plan_sha256") == digest(previous_plan_path.read_text(encoding="utf-8"))
                and artifacts.get("module_tree_sha256")
                == digest((previous_docs / "module_tree.json").read_text(encoding="utf-8"))
            )
            if (
                exact_snapshot
                and exact_generation
                and artifact_hashes_match
                and isinstance(previous_tree, dict)
                and isinstance(previous_plan.get("primary_owner"), dict)
            ):
                page_names = self._tree_page_names(previous_tree)
                required_pages = page_names | {"overview"}
                missing_pages = sorted(
                    name for name in required_pages if not (previous_docs / f"{name}.md").is_file()
                )
                if not missing_pages:
                    docs_dir = Path(self.config.docs_dir).resolve()
                    for name in sorted(required_pages):
                        source = previous_docs / f"{name}.md"
                        destination = docs_dir / source.name
                        shutil.copy2(source, destination)
                        body = destination.read_text(encoding="utf-8")
                        marker = "<!-- codewiki-pega-source-inventory -->"
                        if marker in body:
                            destination.write_text(body.split(marker, 1)[0].rstrip() + "\n", encoding="utf-8")
                    for filename in ("first_module_tree.json", "module_tree.json"):
                        source = previous_docs / filename
                        if source.is_file():
                            shutil.copy2(source, docs_dir / filename)
                    shutil.copy2(previous_plan_path, Path(self.config.docs_dir).resolve().parent / "plan.json")
                    state.update(
                        {
                            "applied": True,
                            "reason": "identical_evidence_and_generation_contract",
                            "regenerated_modules": [],
                            "reused_modules": sorted(page_names),
                            "fallback_reasons": [],
                            "comparison": {
                                "comparison_kind": "unchanged_cached_snapshot",
                                "same_selection_scope": True,
                                "evidence_changed": False,
                                "changes": {
                                    "entities": {"added": [], "removed": [], "changed": []},
                                    "relationships": {"added": [], "removed": [], "changed": []},
                                    "documents": {"added": [], "removed": [], "changed": []},
                                },
                            },
                        }
                    )
                    return state

            previous_provider = PegaGraphProvider(
                None, project=self.provider.project, cache_dir=previous_run / "evidence"
            )
            # The former input may since have changed or moved. Incremental
            # comparison needs its captured hashes and graph inventory, not
            # its old Markdown bytes, so keep the old package as a manifest
            # baseline instead of requiring the current source to match it.
            previous_package = previous_provider.load_snapshot(
                previous_package_path, verify_document_content=False
            )
            _, validated_previous_plan = validate_plan(previous_package, previous_plan)
            if not isinstance(previous_tree, dict):
                raise ValueError("The previous module tree is not an object")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            state["reason"] = "previous_run_invalid"
            state["details"] = str(exc)
            return state

        report = compare_evidence_packages(
            previous_package, package, before_plan=validated_previous_plan
        )
        state["comparison"] = report
        reasons: list[str] = []
        if not report["same_selection_scope"]:
            reasons.append("selection_scope_changed")
        if documented_rule_ids(previous_package) != documented_rule_ids(package):
            reasons.append("documented_rule_set_changed")
        if report["impact_from_before_plan"]["unassigned_entity_ids"]:
            reasons.append("changed_entities_have_no_previous_module_owner")
        if previous_manifest.get("prompt_version") != PEGA_PROMPT_VERSION:
            reasons.append("prompt_version_changed")
        previous_implementation = previous_manifest.get("implementation") or {}
        current_implementation = _implementation_identity()
        if previous_implementation.get("source_hashes") != current_implementation.get("source_hashes"):
            reasons.append("implementation_changed")
        if previous_manifest.get("model") != {
            "provider": self.config.provider,
            "base_url": self.config.llm_base_url,
            "main_model": self.config.main_model,
            "cluster_model": self.config.cluster_model,
            "prompt_caching": self.config.prompt_caching,
        }:
            reasons.append("model_configuration_changed")

        # Revalidate the old module ownership against the new graph/document
        # inventory. The exact snapshot key changes on every source refresh.
        proposal = {
            "source_kind": "pega",
            "project_id": package["project_id"],
            "modules": validated_previous_plan["modules"],
        }
        try:
            new_tree, new_plan = validate_plan(package, proposal)
        except (ValueError, KeyError, TypeError) as exc:
            reasons.append("previous_module_ownership_invalid_for_new_scope")
            state["details"] = str(exc)
            new_tree, new_plan = {}, {}
        if new_plan and new_plan["primary_owner"] != validated_previous_plan["primary_owner"]:
            reasons.append("module_ownership_changed")
        if not isinstance(previous_tree, dict) or set(previous_tree) != set(new_tree):
            reasons.append("module_tree_changed")

        regenerated = set(report["impact_from_before_plan"]["module_names"])
        if report["evidence_changed"] and not regenerated:
            reasons.append("changed_evidence_has_no_module_impact")
        if not regenerated.issubset(previous_tree):
            reasons.append("impact_module_missing_from_previous_tree")
        required_pages = self._tree_page_names(previous_tree) | {"overview"}
        missing_pages = sorted(
            name for name in required_pages if not (previous_docs / f"{name}.md").is_file()
        )
        if missing_pages:
            reasons.append("previous_run_has_missing_pages")
            state["details"] = missing_pages

        state["fallback_reasons"] = reasons
        if reasons:
            return state

        docs_dir = Path(self.config.docs_dir).resolve()
        page_names = self._tree_page_names(previous_tree)
        for name in sorted(page_names | {"overview"}):
            source = previous_docs / f"{name}.md"
            destination = docs_dir / source.name
            shutil.copy2(source, destination)
            body = destination.read_text(encoding="utf-8")
            marker = "<!-- codewiki-pega-source-inventory -->"
            if marker in body:
                destination.write_text(body.split(marker, 1)[0].rstrip() + "\n", encoding="utf-8")

        # Keep the prior agent-created submodule hierarchy when the rule
        # ownership is unchanged. It provides stable page boundaries between
        # updates; changed top-level modules and their descendants are rebuilt.
        old_first_tree = previous_docs / "first_module_tree.json"
        if old_first_tree.is_file():
            shutil.copy2(old_first_tree, docs_dir / "first_module_tree.json")
        else:
            file_manager.save_json(new_tree, str(docs_dir / FIRST_MODULE_TREE_FILENAME))
        file_manager.save_json(previous_tree, str(docs_dir / MODULE_TREE_FILENAME))
        file_manager.save_json(new_plan, str(docs_dir.parent / "plan.json"))

        invalidated: set[str] = set()
        if report["evidence_changed"]:
            for module_name in regenerated:
                invalidated.update(self._subtree_names(previous_tree, module_name))
            invalidated.add("overview")
        for name in invalidated:
            (docs_dir / f"{name}.md").unlink(missing_ok=True)

        state.update(
            {
                "applied": True,
                "regenerated_modules": sorted(regenerated),
                "reused_modules": sorted(page_names - invalidated),
                "fallback_reasons": [],
            }
        )
        return state

    def _check_local_links(self, docs_dir: Path) -> None:
        link_pattern = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
        for page in docs_dir.glob("*.md"):
            content = page.read_text(encoding="utf-8")
            if page.name != OVERVIEW_FILENAME and "../evidence/" not in content:
                raise ValueError(f"Pega module page has no local evidence citation: {page.name}")
            for target in link_pattern.findall(content):
                path = target.split("#", 1)[0]
                if not path or "://" in path or path.startswith("mailto:"):
                    continue
                if path.startswith("/"):
                    raise ValueError(f"Absolute local link in {page.name}: {target}")
                candidate = Path(os.path.abspath(page.parent / path))
                evidence_root = docs_dir.parent / "evidence"
                try:
                    candidate.relative_to(evidence_root)
                except ValueError:
                    pass
                else:
                    if candidate.is_file():
                        continue
                resolved = candidate.resolve()
                try:
                    resolved.relative_to(docs_dir.parent.resolve())
                except ValueError as exc:
                    raise ValueError(f"Link escapes Pega run directory: {target}") from exc
                if not resolved.exists():
                    raise ValueError(f"Broken local link in {page.name}: {target}")

    def _append_source_inventories(
        self,
        docs_dir: Path,
        package: dict[str, Any],
        plan: dict[str, Any],
    ) -> None:
        """Account for every owned rule and source edge in its module page."""
        entities = {entity["id"]: entity for entity in package["entities"]}
        owners: dict[str, list[str]] = {}
        for entity_id, module_name in plan["primary_owner"].items():
            owners.setdefault(module_name, []).append(entity_id)
        for module_name, entity_ids in owners.items():
            page_path = docs_dir / f"{module_name}.md"
            if not page_path.is_file():
                raise FileNotFoundError(f"Missing primary-owner page {page_path}")
            body = page_path.read_text(encoding="utf-8")
            marker = "<!-- codewiki-pega-source-inventory -->"
            if marker in body:
                raise ValueError(f"Writer inserted the reserved source inventory marker in {page_path}")
            lines = [marker, "", "## Source inventory", "", "### Owned rules", ""]
            for entity_id in sorted(entity_ids):
                entity = entities[entity_id]
                document_links = [
                    f"[official document](../evidence/{self.provider.cache.markdown_path(document_id).name})"
                    for document_id in entity["document_ids"]
                    if document_id in package["documents"]
                ]
                if not document_links:
                    raise ValueError(f"Owned rule {entity_id} has no cached official document")
                name = str(entity["name"]).replace("|", "\\|")
                lines.append(
                    f"- **{name}** (`{entity['rule_type']}`, `{entity_id}`): "
                    + ", ".join(document_links)
                )
            outgoing = [
                edge
                for edge in package.get("relationships") or []
                if edge["source_entity_id"] in entity_ids
            ]
            lines.extend(["", "### Directed relationships", ""])
            if not outgoing:
                lines.append("No directed relationships from these rules are in the captured scope.")
            for edge in sorted(outgoing, key=lambda item: item["id"]):
                qualifiers = ", ".join(
                    f"{key}={edge['properties'][key]}"
                    for key in ("http_method", "step_path", "resolution_outcome")
                    if edge["properties"].get(key) not in (None, "")
                )
                receipt = f"../evidence/edges/{digest(edge['id'])[:24]}.md"
                suffix = f" ({qualifiers})" if qualifiers else ""
                lines.append(
                    f"- `{edge['source_entity_id']}` — `{edge['relationship_type']}` → "
                    f"`{edge['target_entity_id']}`: [edge receipt]({receipt}){suffix}"
                )
            page_path.write_text(body.rstrip() + "\n\n" + "\n".join(lines) + "\n", encoding="utf-8")

    async def generate_parent_module_docs(
        self,
        module_path: list[str],
        working_dir: str,
        components: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tree = file_manager.load_json(str(Path(working_dir) / MODULE_TREE_FILENAME))
        if module_path:
            current = tree
            for segment in module_path:
                info = current[segment]
                current = info.get("children", {})
            child_names = list(current)
            name = module_path[-1]
            output_path = Path(working_dir) / f"{name}.md"
        else:
            child_names = list(tree)
            name = self.provider.project
            output_path = Path(working_dir) / OVERVIEW_FILENAME
        if output_path.exists():
            return tree
        child_content = []
        for child_name in child_names:
            child_path = Path(working_dir) / f"{child_name}.md"
            if not child_path.exists():
                raise FileNotFoundError(f"Missing child page for Pega overview: {child_path}")
            text = child_path.read_text(encoding="utf-8")
            excerpt_limit = 1600 if len(child_names) > 8 else 24000
            if len(text) > excerpt_limit:
                text = text[:excerpt_limit] + "\n[Child page excerpt truncated; follow its citations]"
            child_content.append(f"## {child_name} ({child_name}.md)\n{text}")
        prompt = PEGA_OVERVIEW_PROMPT.format(name=name, children="\n\n".join(child_content))
        answer = self.backend.complete(prompt)
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError(f"Empty Pega overview for {name}")
        if "<OVERVIEW>" in answer and "</OVERVIEW>" in answer:
            answer = answer.split("<OVERVIEW>", 1)[1].split("</OVERVIEW>", 1)[0]
        if not answer.strip():
            raise RuntimeError(f"Empty Pega overview for {name}")
        if not module_path and len(child_names) > 8:
            index = "\n\n## Module index\n\n" + "\n".join(
                f"- [{child_name.replace('_', ' ')}]({child_name}.md)" for child_name in child_names
            )
            answer = answer.strip() + index
        output_path.write_text(answer.strip() + "\n", encoding="utf-8")
        return tree

    async def run_pega(
        self,
        package: dict[str, Any],
        planned_modules: dict[str, Any] | None = None,
        *,
        incremental_from: Path | None = None,
        bundle_evidence: bool = False,
        evidence_refresh: dict[str, Any] | None = None,
    ) -> Path:
        docs_dir = Path(self.config.docs_dir).resolve()
        docs_dir.mkdir(parents=True, exist_ok=True)
        if any(docs_dir.iterdir()):
            raise ValueError(f"Pega docs directory must be empty: {docs_dir}")
        # Capture may have inspected boundary relationships while resolving a
        # slice. Keep planning, writers, and receipts strictly inside the
        # immutable package that defines this run's evidence scope.
        self.provider.bind_snapshot(package)
        capture_mode = (
            str((evidence_refresh or {}).get("decision"))
            if evidence_refresh
            else ("live_mcp" if package.get("requests") else "saved_evidence_replay")
        )
        # Cut the live connection at the capture boundary. Any later evidence
        # access must resolve from the selected package and its local cache.
        self.provider.transport = None
        evidence_dir = docs_dir.parent / "evidence"
        self.provider.cache.publish_to(evidence_dir, bundle=bundle_evidence)
        incremental_state = self._prepare_incremental(package, incremental_from)
        self._write_edge_receipts(package.get("relationships") or [], evidence_dir)
        save_evidence_package(package, docs_dir.parent / "evidence-package.json")
        components = self.provider.cached_codewiki_components(package)
        if planned_modules is not None and (
            not isinstance(planned_modules, dict)
            or planned_modules.get("snapshot_key") != package["snapshot_key"]
        ):
            raise ValueError("A supplied Pega plan must name the exact evidence snapshot_key")
        if incremental_state.get("applied"):
            plan = json.loads((docs_dir.parent / "plan.json").read_text(encoding="utf-8"))
            tree = json.loads((docs_dir / MODULE_TREE_FILENAME).read_text(encoding="utf-8"))
        else:
            tree, plan = (
                validate_plan(package, planned_modules)
                if planned_modules is not None
                else (
                    plan_project_modules(package, self.provider, self.backend, self.config.cluster_model or None)
                    if package.get("scope", {}).get("mode") == "project"
                    else plan_modules(package, self.provider, self.backend, self.config.cluster_model or None)
                )
        )
        file_manager.save_json(plan, str(docs_dir.parent / "plan.json"))
        if not incremental_state.get("applied"):
            file_manager.save_json(tree, str(docs_dir / FIRST_MODULE_TREE_FILENAME))
            file_manager.save_json(tree, str(docs_dir / MODULE_TREE_FILENAME))
        await self.generate_module_documentation(components, sorted(components))
        all_edges = [
            {
                "id": edge.id,
                "source_entity_id": edge.source_entity_id,
                "target_entity_id": edge.target_entity_id,
                "relationship_type": edge.relationship_type,
                "document_id": edge.document_id,
                "properties": edge.properties,
            }
            for edge in sorted(self.provider._relationships.values(), key=lambda item: item.id)
        ]
        self._write_edge_receipts(all_edges, evidence_dir)
        projection_tokens = sorted(
            {
                str(entity["properties"]["projection_token"])
                for entity in package["entities"]
                if entity["properties"].get("projection_token")
            }
        )
        if self.provider.transport is None and bundle_evidence:
            evidence_storage = {
                "mode": "bundled_snapshot",
                "snapshot_evidence_relative_path": None,
            }
        elif self.provider.transport is None:
            evidence_storage = {
                "mode": "snapshot_links",
                "snapshot_evidence_relative_path": os.path.relpath(
                    self.provider.cache.root, docs_dir.parent
                ),
            }
        else:
            evidence_storage = {
                "mode": "run_cache",
                "snapshot_evidence_relative_path": None,
            }
        final_manifest = {
            "source_kind": "pega",
            "project_id": package["project_id"],
            "retrieval_mode": "automatic_evidence_cache",
            "capture_mode": capture_mode,
            "generation_mode": "captured_evidence_package",
            "generation_evidence_scope": "captured_evidence_package",
            "evidence_storage": evidence_storage,
            "initial_snapshot_key": package["snapshot_key"],
            "projection_tokens": projection_tokens,
            "prompt_version": PEGA_PROMPT_VERSION,
            "implementation": _implementation_identity(),
            "model": {
                "provider": self.config.provider,
                "base_url": self.config.llm_base_url,
                "main_model": self.config.main_model,
                "cluster_model": self.config.cluster_model,
                "prompt_caching": self.config.prompt_caching,
            },
            "documents": self.provider.cache.manifest(),
            "relationships": {
                edge["id"]: {
                    "receipt_path": f"evidence/edges/{digest(edge['id'])[:24]}.md",
                    "sha256": digest(stable_json(edge)),
                }
                for edge in all_edges
            },
            "mcp_requests": self.provider._requests,
            "capture_requests": package.get("requests") or [],
            "evidence_refresh": evidence_refresh or {"decision": "saved_evidence_replay"},
            "generation_artifacts": {
                "plan_sha256": digest((docs_dir.parent / "plan.json").read_text(encoding="utf-8")),
                "module_tree_sha256": digest(
                    (docs_dir / MODULE_TREE_FILENAME).read_text(encoding="utf-8")
                ),
            },
            "evidence_reads": self.provider._evidence_reads,
            "model_calls": self.backend.usage_events,
            "specialist_calls": self.provider._specialist_events,
            "incremental_update": {
                key: incremental_state.get(key)
                for key in (
                    "enabled", "applied", "source_run", "reason", "details",
                    "fallback_reasons", "regenerated_modules", "reused_modules",
                )
            },
        }
        comparison = incremental_state.get("comparison")
        if isinstance(comparison, dict):
            final_manifest["incremental_update"]["changes"] = comparison["changes"]
            final_manifest["incremental_update"]["comparison_kind"] = comparison["comparison_kind"]
            final_manifest["incremental_update"]["evidence_changed"] = comparison["evidence_changed"]
        save_evidence_package(final_manifest, docs_dir.parent / "evidence-manifest.json")
        self.create_documentation_metadata(str(docs_dir), components, len(components))
        missing = self.validate_generated_docs(str(docs_dir))
        if missing:
            raise IncompleteDocumentationError(missing)
        self._append_source_inventories(docs_dir, package, plan)
        self._check_local_links(docs_dir)
        return docs_dir
