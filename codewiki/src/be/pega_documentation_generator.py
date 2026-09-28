"""Pega source mode for CodeWiki's module writer and overview workflow."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from codewiki.src.be.documentation_generator import DocumentationGenerator, IncompleteDocumentationError
from codewiki.src.be.pydantic_ai_backend import PydanticAIBackend
from codewiki.src.be.pega_planner import plan_modules, validate_plan
from codewiki.src.be.pega_prompts import PEGA_OVERVIEW_PROMPT, PEGA_PROMPT_VERSION
from codewiki.src.be.sources.evidence import digest, stable_json
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
        "codewiki/src/be/documentation_generator.py",
        "codewiki/src/be/pega_documentation_generator.py",
        "codewiki/src/be/pega_planner.py",
        "codewiki/src/be/pega_prompts.py",
        "codewiki/src/be/pydantic_ai_backend.py",
        "codewiki/src/be/sources/evidence.py",
        "codewiki/src/be/sources/pega_mcp.py",
        "codewiki/src/be/utils.py",
        "codewiki/src/config.py",
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

    def _write_edge_receipts(self, edges: list[dict[str, Any]]) -> None:
        edge_dir = self.provider.cache.root / "edges"
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
                resolved = (page.parent / path).resolve()
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
            if len(text) > 24000:
                text = text[:24000] + "\n[Child page excerpt truncated; follow its citations]"
            child_content.append(f"## {child_name} ({child_name}.md)\n{text}")
        prompt = PEGA_OVERVIEW_PROMPT.format(name=name, children="\n\n".join(child_content))
        answer = self.backend.complete(prompt)
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError(f"Empty Pega overview for {name}")
        if "<OVERVIEW>" in answer and "</OVERVIEW>" in answer:
            answer = answer.split("<OVERVIEW>", 1)[1].split("</OVERVIEW>", 1)[0]
        if not answer.strip():
            raise RuntimeError(f"Empty Pega overview for {name}")
        output_path.write_text(answer.strip() + "\n", encoding="utf-8")
        return tree

    async def run_pega(
        self, package: dict[str, Any], planned_modules: dict[str, Any] | None = None
    ) -> Path:
        docs_dir = Path(self.config.docs_dir).resolve()
        docs_dir.mkdir(parents=True, exist_ok=True)
        if any(docs_dir.iterdir()):
            raise ValueError(f"Pega docs directory must be empty: {docs_dir}")
        self._write_edge_receipts(package.get("relationships") or [])
        save_evidence_package(package, docs_dir.parent / "evidence-package.json")
        components = self.provider.codewiki_components(package)
        if planned_modules is not None and (
            not isinstance(planned_modules, dict)
            or planned_modules.get("snapshot_key") != package["snapshot_key"]
        ):
            raise ValueError("A supplied Pega plan must name the exact evidence snapshot_key")
        tree, plan = (
            validate_plan(package, planned_modules)
            if planned_modules is not None
            else plan_modules(package, self.provider, self.backend, self.config.cluster_model or None)
        )
        file_manager.save_json(plan, str(docs_dir.parent / "plan.json"))
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
        self._write_edge_receipts(all_edges)
        projection_tokens = sorted(
            {
                str(entity["properties"]["projection_token"])
                for entity in package["entities"]
                if entity["properties"].get("projection_token")
            }
        )
        final_manifest = {
            "source_kind": "pega",
            "project_id": package["project_id"],
            "retrieval_mode": (
                "frozen_snapshot" if self.provider.transport is None else "live_mcp"
            ),
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
            "model_calls": self.backend.usage_events,
            "specialist_calls": self.provider._specialist_events,
        }
        save_evidence_package(final_manifest, docs_dir.parent / "evidence-manifest.json")
        self.create_documentation_metadata(str(docs_dir), components, len(components))
        missing = self.validate_generated_docs(str(docs_dir))
        if missing:
            raise IncompleteDocumentationError(missing)
        self._append_source_inventories(docs_dir, package, plan)
        self._check_local_links(docs_dir)
        return docs_dir
