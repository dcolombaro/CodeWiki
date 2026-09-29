"""Capture a Pega evidence slice through the project-owned MCP service."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import click

from codewiki.src.be.sources.pega_mcp import (
    PegaGraphProvider,
    PegaMCPClient,
    save_evidence_package,
)


async def _resolve_seed(
    provider: PegaGraphProvider,
    seed_id: str | None,
    seed_name: str | None,
    rule_type: str | None,
    class_name: str | None,
    ruleset: str | None,
) -> str:
    if bool(seed_id) == bool(seed_name):
        raise click.UsageError("Provide exactly one of --seed-id or --seed-name")
    if seed_id:
        return seed_id
    assert seed_name is not None
    matches = await provider.search_entities(
        seed_name,
        rule_types=[rule_type] if rule_type else None,
        class_names=[class_name] if class_name else None,
        rulesets=[ruleset] if ruleset else None,
    )
    exact = [item for item in matches if item.name.casefold() == seed_name.casefold()]
    if len(exact) != 1:
        identities = [item.card() for item in exact or matches]
        raise click.ClickException(
            f"Seed resolution returned {len(exact)} exact matches; "
            f"specify --seed-id. Candidates: {identities[:10]}"
        )
    return exact[0].id


@click.command("pega-snapshot")
@click.option("--project", required=True, help="Exact ID from PEGA projects/projects.yaml")
@click.option("--mcp-command", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--mcp-arg", multiple=True, help="Repeat for each server argument, e.g. --mcp-arg=-m")
@click.option("--mcp-cwd", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--seed-id", help="Exact project-prefixed graph entity ID")
@click.option("--seed-name", help="Resolve one exact entity name before capturing")
@click.option("--rule-type", help="Optional rule type filter for --seed-name")
@click.option("--class-name", help="Optional Applies-To class filter for --seed-name")
@click.option("--ruleset", help="Optional ruleset filter for --seed-name")
@click.option("--relationship-type", multiple=True, help="Repeat for each traversed edge type")
@click.option("--expand-entity-id", multiple=True, help="Expand one selected branch by one further edge")
@click.option("--depth", default=1, type=click.IntRange(1, 8), show_default=True)
@click.option("--max-documents", default=40, type=click.IntRange(1, 500), show_default=True)
@click.option("--output", required=True, type=click.Path(path_type=Path))
def pega_snapshot_command(
    project: str,
    mcp_command: str,
    mcp_arg: tuple[str, ...],
    mcp_cwd: str,
    seed_id: str | None,
    seed_name: str | None,
    rule_type: str | None,
    class_name: str | None,
    ruleset: str | None,
    relationship_type: tuple[str, ...],
    expand_entity_id: tuple[str, ...],
    depth: int,
    max_documents: int,
    output: Path,
) -> None:
    """Save graph-selected official Markdown and directed evidence for one slice."""
    if bool(seed_id) == bool(seed_name):
        raise click.UsageError("Provide exactly one of --seed-id or --seed-name")
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise click.ClickException(f"Output directory must be empty: {output}")

    async def capture() -> dict:
        async with PegaMCPClient(mcp_command, list(mcp_arg), cwd=mcp_cwd) as client:
            provider = PegaGraphProvider(client, project=project, cache_dir=output / "evidence")
            await provider.initialize()
            resolved_id = await _resolve_seed(
                provider, seed_id, seed_name, rule_type, class_name, ruleset
            )
            package = await provider.snapshot_slice(
                seed_entity_id=resolved_id,
                depth=depth,
                relationship_types=list(relationship_type) or None,
                expand_entity_ids=list(expand_entity_id),
                max_documents=max_documents,
            )
            save_evidence_package(package, output / "evidence-package.json")
            return package

    try:
        package = asyncio.run(capture())
    except (RuntimeError, ValueError, KeyError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"Saved {package['scope']['entity_count']} entities, "
        f"{len(package['relationships'])} directed relationships and "
        f"{package['scope']['document_count']} official documents to {output}"
    )


@click.command("pega-plan")
@click.option("--project", required=True, help="Exact PEGA project ID")
@click.option("--snapshot-dir", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--plan-file", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", required=True, type=click.Path(path_type=Path))
def pega_plan_command(project: str, snapshot_dir: Path, plan_file: Path, output: Path) -> None:
    """Validate a frozen evidence slice and materialize its CodeWiki module tree."""
    from codewiki.src.be.pega_planner import documented_rule_ids, validate_plan
    from codewiki.src.config import MODULE_TREE_FILENAME

    output = output.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise click.ClickException(f"Output directory must be empty: {output}")
    try:
        provider = PegaGraphProvider(
            None, project=project, cache_dir=snapshot_dir.resolve() / "evidence"
        )
        package = provider.load_snapshot(snapshot_dir / "evidence-package.json")
        proposed = json.loads(plan_file.read_text(encoding="utf-8"))
        if not isinstance(proposed, dict) or proposed.get("snapshot_key") != package["snapshot_key"]:
            raise ValueError("The reviewed plan must name the exact evidence snapshot_key")
        tree, plan = validate_plan(package, proposed)
        components = provider.codewiki_components(package)
        if set(components) != documented_rule_ids(package):
            raise ValueError("CodeWiki component view does not cover all documented rules")
        component_view = {
            entity_id: {
                "name": node.name,
                "component_type": node.component_type,
                "depends_on": sorted(node.depends_on),
                "evidence_card": json.loads(node.source_code or "{}"),
            }
            for entity_id, node in sorted(components.items())
        }
        save_evidence_package(plan, output / "plan.json")
        save_evidence_package(tree, output / MODULE_TREE_FILENAME)
        save_evidence_package(component_view, output / "components.json")
        save_evidence_package(
            {
                "project_id": project,
                "snapshot_key": package["snapshot_key"],
                "scope_complete": package["scope"]["complete_for_requested_scope"],
                "entity_count": len(package["entities"]),
                "relationship_count": len(package["relationships"]),
                "verified_document_count": len(package["documents"]),
                "primary_owner_count": len(plan["primary_owner"]),
                "module_count": len(tree),
                "unresolved_references": package.get("unresolved_references") or [],
            },
            output / "validation.json",
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"Validated {len(components)} CodeWiki components in {len(tree)} modules; "
        f"saved plan and tree to {output}"
    )


@click.command("pega-compare")
@click.option("--project", required=True, help="Exact PEGA project ID")
@click.option("--before-dir", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--after-dir", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--before-plan", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", required=True, type=click.Path(path_type=Path))
def pega_compare_command(
    project: str,
    before_dir: Path,
    after_dir: Path,
    before_plan: Path | None,
    output: Path,
) -> None:
    """Compare two verified evidence snapshots and locate affected modules."""
    from codewiki.src.be.sources.pega_diff import compare_evidence_packages

    output = output.resolve()
    if output.exists():
        raise click.ClickException(f"Comparison output already exists: {output}")
    try:
        before_provider = PegaGraphProvider(
            None, project=project, cache_dir=before_dir.resolve() / "evidence"
        )
        after_provider = PegaGraphProvider(
            None, project=project, cache_dir=after_dir.resolve() / "evidence"
        )
        before = before_provider.load_snapshot(before_dir / "evidence-package.json")
        after = after_provider.load_snapshot(after_dir / "evidence-package.json")
        plan = json.loads(before_plan.read_text(encoding="utf-8")) if before_plan else None
        if plan is not None:
            from codewiki.src.be.pega_planner import validate_plan

            _, plan = validate_plan(before, plan)
        report = compare_evidence_packages(before, after, before_plan=plan)
        save_evidence_package(report, output)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"Saved {report['comparison_kind']} report to {output}; "
        f"{len(report['impact_from_before_plan']['module_names'])} existing modules need review"
    )


@click.command("pega-generate")
@click.option("--project", required=True, help="Exact ID from PEGA projects/projects.yaml")
@click.option("--snapshot-dir", type=click.Path(exists=True, file_okay=False, path_type=Path), help="Replay exact saved evidence without live MCP retrieval")
@click.option("--mcp-command", type=click.Path(exists=True, dir_okay=False))
@click.option("--mcp-arg", multiple=True)
@click.option("--mcp-cwd", type=click.Path(exists=True, file_okay=False))
@click.option("--seed-id")
@click.option("--seed-name")
@click.option("--rule-type")
@click.option("--class-name")
@click.option("--ruleset")
@click.option("--relationship-type", multiple=True)
@click.option("--expand-entity-id", multiple=True, help="Expand one selected branch by one further edge")
@click.option("--depth", default=1, type=click.IntRange(1, 8), show_default=True)
@click.option("--max-documents", default=40, type=click.IntRange(1, 500), show_default=True)
@click.option("--model", required=True, help="Model ID on the customer approved API endpoint")
@click.option("--cluster-model", default=None, help="Optional model for module planning")
@click.option("--model-base-url", required=True, help="Explicit customer approved API base URL")
@click.option("--api-key-env", required=True, help="Name of the environment variable holding the API key")
@click.option("--plan-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Optional reviewed module plan for this exact snapshot")
@click.option("--output", required=True, type=click.Path(path_type=Path))
def pega_generate_command(
    project: str,
    snapshot_dir: Path | None,
    mcp_command: str | None,
    mcp_arg: tuple[str, ...],
    mcp_cwd: str | None,
    seed_id: str | None,
    seed_name: str | None,
    rule_type: str | None,
    class_name: str | None,
    ruleset: str | None,
    relationship_type: tuple[str, ...],
    expand_entity_id: tuple[str, ...],
    depth: int,
    max_documents: int,
    model: str,
    cluster_model: str | None,
    model_base_url: str,
    api_key_env: str,
    plan_file: Path | None,
    output: Path,
) -> None:
    """Generate an evidence-linked CodeWiki from a frozen Pega graph slice."""
    if snapshot_dir is not None:
        if any((seed_id, seed_name, rule_type, class_name, ruleset, mcp_command, mcp_arg, mcp_cwd, relationship_type, expand_entity_id)):
            raise click.UsageError(
                "--snapshot-dir cannot be combined with live MCP or seed options"
            )
    elif bool(seed_id) == bool(seed_name) or not mcp_command or not mcp_cwd:
        raise click.UsageError(
            "Live generation requires --mcp-command, --mcp-cwd, and exactly one seed"
        )
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise click.ClickException(f"API key variable {api_key_env} is unset")
    if not model_base_url.startswith(("https://", "http://")):
        raise click.UsageError("--model-base-url must be an explicit HTTP(S) URL")
    output = output.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise click.ClickException(f"Output directory must be empty: {output}")
    # The Pega run does not send diagrams to CodeWiki's default remote renderer.
    os.environ["MERMAID_VALIDATE"] = "0"

    async def generate() -> Path:
        from codewiki.src.be.pega_documentation_generator import PegaDocumentationGenerator
        from codewiki.src.config import Config

        async def write_pages(provider: PegaGraphProvider, package: dict, source_path: str) -> Path:
            config = Config.from_cli(
                repo_path=source_path,
                output_dir=str(output / "docs"),
                llm_base_url=model_base_url,
                llm_api_key=api_key,
                main_model=model,
                cluster_model=cluster_model or model,
                fallback_model=model,
                prompt_caching=(model_base_url.rstrip("/") != "https://api.openai.com/v1"),
                artifacts_enabled=False,
                with_prose=False,
            )
            config.source_kind = "pega"
            config.pega_project_id = project
            config.pega_snapshot_key = package["snapshot_key"]
            generator = PegaDocumentationGenerator(config, provider)
            planned_modules = json.loads(plan_file.read_text(encoding="utf-8")) if plan_file else None
            return await generator.run_pega(package, planned_modules)

        if snapshot_dir is not None:
            captured_dir = snapshot_dir.resolve()
            captured = PegaGraphProvider(
                None, project=project, cache_dir=captured_dir / "evidence"
            )
            package = captured.load_snapshot(captured_dir / "evidence-package.json")
            provider = PegaGraphProvider(
                None, project=project, cache_dir=output / "evidence"
            )
            for document_id in package["documents"]:
                document = captured.cache.get_document(document_id)
                if document is None:
                    raise ValueError(f"Missing cached official document {document_id}")
                provider.cache.put_document(document)
            save_evidence_package(package, output / "evidence-package.json")
            provider.load_snapshot(output / "evidence-package.json")
            return await write_pages(provider, package, str(captured_dir))

        assert mcp_command is not None and mcp_cwd is not None
        async with PegaMCPClient(
            mcp_command, list(mcp_arg), cwd=mcp_cwd, exclude_env={api_key_env}
        ) as client:
            provider = PegaGraphProvider(client, project=project, cache_dir=output / "evidence")
            await provider.initialize()
            resolved_id = await _resolve_seed(
                provider, seed_id, seed_name, rule_type, class_name, ruleset
            )
            package = await provider.snapshot_slice(
                seed_entity_id=resolved_id,
                depth=depth,
                relationship_types=list(relationship_type) or None,
                expand_entity_ids=list(expand_entity_id),
                max_documents=max_documents,
            )
            return await write_pages(provider, package, mcp_cwd)

    try:
        docs_path = asyncio.run(generate())
    except (RuntimeError, ValueError, KeyError, FileNotFoundError) as exc:
        raise click.ClickException(str(exc)) from exc
    from codewiki.cli.pega_viewer import render_pega_viewer

    try:
        viewer_path = render_pega_viewer(output)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Markdown saved to {docs_path}, but HTML viewer failed: {exc}") from exc
    click.echo(f"Pega CodeWiki saved to {docs_path}; local viewer: {viewer_path}")


@click.command("pega-viewer")
@click.option(
    "--run-dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Existing pega-generate output directory",
)
def pega_viewer_command(run_dir: Path) -> None:
    """Build or refresh a local HTML viewer without model or MCP calls."""
    from codewiki.cli.pega_viewer import render_pega_viewer

    try:
        output = render_pega_viewer(run_dir)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Local PEGA viewer saved to {output}")
