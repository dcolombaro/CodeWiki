"""Chat-driven PEGA capture and CodeWiki generation."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"The CodeWiki PEGA MCP launcher must set {name}")
    return value


def _output_directory(arguments: dict[str, Any]) -> Path:
    output_arg = arguments.get("output_dir")
    if output_arg:
        output = Path(str(output_arg)).expanduser().resolve()
    else:
        root = Path(os.environ.get("CODEWIKI_PEGA_OUTPUT_ROOT", "runs")).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = root.resolve() / f"pega-chat-{timestamp}-{uuid4().hex[:8]}"
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    return output


async def _generate(arguments: dict[str, Any]) -> dict[str, Any]:
    from codewiki.cli.pega_viewer import render_pega_viewer
    from codewiki.src.be.pega_documentation_generator import PegaDocumentationGenerator
    from codewiki.src.be.sources.pega_mcp import PegaGraphProvider, PegaMCPClient, save_evidence_package
    from codewiki.src.be.pega_refresh import PegaEvidenceStore, prepare_pega_evidence, scope_request
    from codewiki.src.config import Config

    project = str(arguments.get("project") or _required_env("PEGA_PROJECT_ID"))
    api_key = _required_env("CUSTOMER_MODEL_API_KEY")
    base_url = _required_env("CUSTOMER_MODEL_BASE_URL")
    model = _required_env("CUSTOMER_MODEL_ID")
    if not base_url.startswith(("https://", "http://")):
        raise ValueError("CUSTOMER_MODEL_BASE_URL must be an explicit HTTP(S) URL")

    kb_root = Path(_required_env("PEGA_KB_ROOT")).expanduser().resolve()
    pega_python = Path(_required_env("PEGA_PYTHON")).expanduser().resolve()
    if not kb_root.is_dir():
        raise ValueError(f"PEGA_KB_ROOT is not a directory: {kb_root}")
    if not pega_python.is_file():
        raise ValueError(f"PEGA_PYTHON does not exist: {pega_python}")

    raw_args = os.environ.get("PEGA_MCP_ARGS_JSON", '["-m", "pega_kb.neo4j_mcp"]')
    mcp_args = json.loads(raw_args)
    if not isinstance(mcp_args, list) or any(not isinstance(item, str) for item in mcp_args):
        raise ValueError("PEGA_MCP_ARGS_JSON must be a JSON array of strings")

    seed_id = str(arguments["seed_id"]).strip() if arguments.get("seed_id") else None
    seed_name = str(arguments["seed_name"]).strip() if arguments.get("seed_name") else None
    if seed_id and seed_name:
        raise ValueError("Provide only one of seed_id or seed_name")
    depth = int(arguments.get("depth", 1))
    max_documents = int(arguments.get("max_documents", 40))
    if not 1 <= depth <= 8:
        raise ValueError("depth must be between 1 and 8")
    if not 1 <= max_documents <= 500:
        raise ValueError("max_documents must be between 1 and 500")
    relationship_types = arguments.get("relationship_types") or []
    expand_entity_ids = arguments.get("expand_entity_ids") or []
    replan = bool(arguments.get("replan", False))
    if not isinstance(relationship_types, list) or any(not isinstance(item, str) for item in relationship_types):
        raise ValueError("relationship_types must be an array of strings")
    if not isinstance(expand_entity_ids, list) or any(not isinstance(item, str) for item in expand_entity_ids):
        raise ValueError("expand_entity_ids must be an array of strings")
    if not (seed_id or seed_name) and (
        depth != 1
        or relationship_types
        or expand_entity_ids
        or max_documents != 40
        or arguments.get("rule_type")
        or arguments.get("class_name")
        or arguments.get("ruleset")
    ):
        raise ValueError(
            "seed filters, depth, relationship_types, expand_entity_ids, and max_documents require a seed"
        )

    output = _output_directory(arguments)
    os.environ["MERMAID_VALIDATE"] = "0"
    request = scope_request(
        seed_id=seed_id,
        seed_name=seed_name,
        rule_type=arguments.get("rule_type"),
        class_name=arguments.get("class_name"),
        ruleset=arguments.get("ruleset"),
        depth=depth,
        relationship_types=relationship_types,
        expand_entity_ids=expand_entity_ids,
        max_documents=max_documents,
    )
    output_root = Path(os.environ.get("CODEWIKI_PEGA_OUTPUT_ROOT", "runs")).expanduser()
    if not output_root.is_absolute():
        output_root = Path.cwd() / output_root
    cache_root = Path(
        os.environ.get("CODEWIKI_PEGA_CACHE_DIR", str(output_root / ".codewiki-pega-cache"))
    ).expanduser()
    evidence_store = PegaEvidenceStore(cache_root, project)
    package_path = output / "evidence-package.json"

    async with PegaMCPClient(
        str(pega_python), mcp_args, cwd=str(kb_root), exclude_env={"CUSTOMER_MODEL_API_KEY"}
    ) as client:
        capture, package, evidence_refresh = await prepare_pega_evidence(
            project=project,
            client=client,
            source_root=kb_root,
            store=evidence_store,
            request=request,
        )

    # Planning and writing consume the same package regardless of whether it
    # was refreshed, reused, or derived from a complete cached project graph.
    save_evidence_package(package, package_path)
    frozen = PegaGraphProvider(
        None, project=project, cache_dir=capture.cache.root, source_root=kb_root
    )
    package = frozen.load_snapshot(package_path)
    config = Config.from_cli(
        repo_path=str(kb_root),
        output_dir=str(output / "docs"),
        llm_base_url=base_url,
        llm_api_key=api_key,
        main_model=model,
        cluster_model=model,
        fallback_model=model,
        prompt_caching=(base_url.rstrip("/") != "https://api.openai.com/v1"),
        artifacts_enabled=False,
        with_prose=False,
    )
    config.source_kind = "pega"
    config.pega_project_id = project
    config.pega_snapshot_key = package["snapshot_key"]
    generator = PegaDocumentationGenerator(config, frozen)
    previous_run = None if replan else evidence_store.latest_run(request)
    docs_path = await generator.run_pega(
        package,
        incremental_from=previous_run,
        bundle_evidence=False,
        evidence_refresh=evidence_refresh,
    )
    viewer_path = render_pega_viewer(output)
    evidence_store.record_run(request, output)
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    return {
        "status": "complete",
        "project_id": project,
        "scope_mode": package["scope"].get("mode"),
        "seed_entity_ids": package["scope"].get("seed_entity_ids", []),
        "entities": package["scope"].get("entity_count", len(package["entities"])),
        "relationships": len(package["relationships"]),
        "official_documents": package["scope"].get("document_count", len(package["documents"])),
        "modules": len(plan.get("modules", [])),
        "output_dir": str(output),
        "evidence_package": str(package_path),
        "docs_dir": str(docs_path),
        "viewer": str(viewer_path),
        "evidence_refresh": evidence_refresh,
    }


def handle_generate_pega_docs(arguments: dict[str, Any]) -> str:
    """Run capture and writing in a worker thread with its own event loop."""
    return json.dumps(asyncio.run(_generate(arguments)), ensure_ascii=False, indent=2)
