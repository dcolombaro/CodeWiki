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
@click.option("--release", default="lead", show_default=True, help="Application release slug or ID; leadtest is a separate capture")
@click.option("--mcp-command", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--mcp-arg", multiple=True, help="Repeat for each server argument, e.g. --mcp-arg=-m")
@click.option("--mcp-cwd", required=True, type=click.Path(exists=True, file_okay=False))
@click.option("--seed-id", help="Optional focus: exact project-prefixed graph entity ID")
@click.option("--seed-name", help="Optional focus: resolve one exact entity name")
@click.option("--rule-type", help="Optional rule type filter for --seed-name")
@click.option("--class-name", help="Optional Applies-To class filter for --seed-name")
@click.option("--ruleset", help="Optional ruleset filter for --seed-name")
@click.option("--relationship-type", multiple=True, help="Repeat for each traversed edge type")
@click.option("--relation-kind", multiple=True, help="Repeat for Pega relation_kind values such as CALLS_ACTIVITY")
@click.option("--expand-entity-id", multiple=True, help="Expand one selected branch by one further edge")
@click.option("--depth", default=1, type=click.IntRange(1, 8), show_default=True, help="Focused slice only")
@click.option("--max-documents", default=40, type=click.IntRange(1, 500), show_default=True, help="Focused slice only")
@click.option("--output", required=True, type=click.Path(path_type=Path))
def pega_snapshot_command(
    project: str,
    release: str,
    mcp_command: str,
    mcp_arg: tuple[str, ...],
    mcp_cwd: str,
    seed_id: str | None,
    seed_name: str | None,
    rule_type: str | None,
    class_name: str | None,
    ruleset: str | None,
    relationship_type: tuple[str, ...],
    relation_kind: tuple[str, ...],
    expand_entity_id: tuple[str, ...],
    depth: int,
    max_documents: int,
    output: Path,
) -> None:
    """Capture the whole Pega project, or an optional focused slice."""
    if seed_id and seed_name:
        raise click.UsageError("Provide at most one of --seed-id or --seed-name")
    if not (seed_id or seed_name) and any(
        (rule_type, class_name, ruleset, relationship_type, relation_kind, expand_entity_id, depth != 1, max_documents != 40)
    ):
        raise click.UsageError("Seed, depth, relationship, and document-limit options require --seed-id or --seed-name")
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise click.ClickException(f"Output directory must be empty: {output}")

    async def capture() -> dict:
        async with PegaMCPClient(mcp_command, list(mcp_arg), cwd=mcp_cwd) as client:
            provider = PegaGraphProvider(
                client,
                project=project,
                release=release,
                cache_dir=output / "evidence",
                source_root=Path(mcp_cwd),
            )
            await provider.initialize()
            await provider.project_projection_tokens()
            provider._require_ready_status()
            initial_revision = provider.project_revision
            if not initial_revision:
                raise RuntimeError("Cannot determine the current PEGA project revision")
            if seed_id or seed_name:
                resolved_id = await _resolve_seed(
                    provider, seed_id, seed_name, rule_type, class_name, ruleset
                )
                package = await provider.snapshot_slice(
                    seed_entity_id=resolved_id,
                    depth=depth,
                    relationship_types=list(relationship_type) or None,
                    relation_kinds=list(relation_kind) or None,
                    expand_entity_ids=list(expand_entity_id),
                    max_documents=max_documents,
                )
            else:
                package = await provider.snapshot_project()
            await provider._verify_status_unchanged()
            await provider.project_projection_tokens()
            provider._require_ready_status()
            if provider.project_revision != initial_revision:
                raise RuntimeError(
                    "PEGA project revision changed during evidence capture; retry snapshot"
                )
            from codewiki.src.be.pega_refresh import stamp_project_revision

            stamp_project_revision(package, initial_revision)
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
@click.option("--release", default="lead", show_default=True, help="Application release slug or ID; leadtest is a separate wiki")
@click.option("--snapshot-dir", type=click.Path(exists=True, file_okay=False, path_type=Path), help="Replay exact saved evidence without live MCP retrieval")
@click.option("--mcp-command", type=click.Path(exists=True, dir_okay=False))
@click.option("--mcp-arg", multiple=True)
@click.option("--mcp-cwd", type=click.Path(exists=True, file_okay=False))
@click.option("--seed-id", help="Optional focus: exact project-prefixed graph entity ID")
@click.option("--seed-name", help="Optional focus: resolve one exact entity name")
@click.option("--rule-type")
@click.option("--class-name")
@click.option("--ruleset")
@click.option("--relationship-type", multiple=True)
@click.option("--relation-kind", multiple=True, help="Pega relation_kind values to follow, e.g. CALLS_ACTIVITY")
@click.option("--expand-entity-id", multiple=True, help="Expand one selected branch by one further edge")
@click.option("--depth", default=1, type=click.IntRange(1, 8), show_default=True, help="Focused slice only")
@click.option("--max-documents", default=40, type=click.IntRange(1, 500), show_default=True, help="Focused slice only")
@click.option("--model", required=True, help="Model ID on the customer approved API endpoint")
@click.option("--cluster-model", default=None, help="Optional model for module planning")
@click.option("--model-base-url", required=True, help="Explicit customer approved API base URL")
@click.option("--api-key-env", required=True, help="Name of the environment variable holding the API key")
@click.option("--plan-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Optional reviewed module plan for this exact snapshot")
@click.option(
    "--incremental-from",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Reuse unaffected pages from a prior PEGA wiki run",
)
@click.option(
    "--bundle-evidence",
    is_flag=True,
    help="Copy frozen evidence files so the run can move without its snapshot",
)
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Persistent PEGA evidence cache (default: CODEWIKI_PEGA_CACHE_DIR or a cache beside the output)",
)
@click.option(
    "--replan",
    is_flag=True,
    help="Reuse current evidence but re-run planning and all page writers",
)
@click.option(
    "--resume-existing",
    is_flag=True,
    help="Resume an interrupted live run after verifying its evidence snapshot and plan",
)
@click.option("--output", required=True, type=click.Path(path_type=Path))
def pega_generate_command(
    project: str,
    release: str,
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
    relation_kind: tuple[str, ...],
    expand_entity_id: tuple[str, ...],
    depth: int,
    max_documents: int,
    model: str,
    cluster_model: str | None,
    model_base_url: str,
    api_key_env: str,
    plan_file: Path | None,
    incremental_from: Path | None,
    bundle_evidence: bool,
    cache_dir: Path | None,
    replan: bool,
    resume_existing: bool,
    output: Path,
) -> None:
    """Generate an evidence-linked wiki for a Pega project or focused slice."""
    if snapshot_dir is not None:
        if any((seed_id, seed_name, rule_type, class_name, ruleset, mcp_command, mcp_arg, mcp_cwd, relationship_type, relation_kind, expand_entity_id)):
            raise click.UsageError(
                "--snapshot-dir cannot be combined with live MCP or seed options"
            )
    elif (seed_id and seed_name) or not mcp_command or not mcp_cwd:
        raise click.UsageError(
            "Live generation requires --mcp-command and --mcp-cwd; provide at most one seed"
        )
    if snapshot_dir is None and not (seed_id or seed_name) and any(
        (rule_type, class_name, ruleset, relationship_type, relation_kind, expand_entity_id, depth != 1, max_documents != 40)
    ):
        raise click.UsageError("Seed, depth, relationship, and document-limit options require --seed-id or --seed-name")
    if incremental_from is not None and plan_file is not None:
        raise click.UsageError(
            "--incremental-from reuses the previous run's validated module ownership; "
            "do not combine it with --plan-file"
        )
    if replan and incremental_from is not None:
        raise click.UsageError("--replan cannot be combined with --incremental-from")
    if resume_existing and any((snapshot_dir, incremental_from, plan_file, replan, bundle_evidence)):
        raise click.UsageError(
            "--resume-existing requires live evidence and the run's existing plan; "
            "do not combine it with snapshot, incremental, plan, replan, or bundled-evidence options"
        )
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise click.ClickException(f"API key variable {api_key_env} is unset")
    if not model_base_url.startswith(("https://", "http://")):
        raise click.UsageError("--model-base-url must be an explicit HTTP(S) URL")
    output = output.resolve()
    cache_base = Path(
        os.environ.get("CODEWIKI_PEGA_OUTPUT_ROOT", str(output.parent))
    ).expanduser()
    if not cache_base.is_absolute():
        cache_base = Path.cwd() / cache_base
    configured_cache = cache_dir or (
        Path(os.environ["CODEWIKI_PEGA_CACHE_DIR"])
        if os.environ.get("CODEWIKI_PEGA_CACHE_DIR")
        else cache_base / ".codewiki-pega-cache"
    )
    configured_cache = configured_cache.expanduser().resolve()
    if output.exists() and (not output.is_dir() or (any(output.iterdir()) and not resume_existing)):
        raise click.ClickException(f"Output directory must be empty: {output}")
    if resume_existing and not output.is_dir():
        raise click.ClickException(f"Cannot resume; output directory does not exist: {output}")
    # The Pega run does not send diagrams to CodeWiki's default remote renderer.
    os.environ["MERMAID_VALIDATE"] = "0"

    async def generate() -> Path:
        from codewiki.src.be.pega_documentation_generator import PegaDocumentationGenerator
        from codewiki.src.config import Config

        async def write_pages(
            provider: PegaGraphProvider,
            package: dict,
            source_path: str,
            evidence_refresh: dict | None = None,
            previous_run: Path | None = None,
            resume_existing: bool = False,
        ) -> Path:
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
            planned_modules = (
                json.loads((output / "plan.json").read_text(encoding="utf-8"))
                if resume_existing
                else (json.loads(plan_file.read_text(encoding="utf-8")) if plan_file else None)
            )
            return await generator.run_pega(
                package,
                planned_modules,
                incremental_from=previous_run,
                bundle_evidence=bundle_evidence,
                evidence_refresh=evidence_refresh,
                resume_existing=resume_existing,
            )

        if snapshot_dir is not None:
            captured_dir = snapshot_dir.resolve()
            captured = PegaGraphProvider(
                None, project=project, release=release, cache_dir=captured_dir / "evidence"
            )
            package = captured.load_snapshot(captured_dir / "evidence-package.json")
            return await write_pages(
                captured,
                package,
                str(captured_dir),
                {"decision": "saved_evidence_replay"},
                incremental_from,
                resume_existing,
            )

        assert mcp_command is not None and mcp_cwd is not None
        from codewiki.src.be.pega_refresh import PegaEvidenceStore, prepare_pega_evidence, scope_request

        request = scope_request(
            release=release,
            seed_id=seed_id,
            seed_name=seed_name,
            rule_type=rule_type,
            class_name=class_name,
            ruleset=ruleset,
            depth=depth,
            relationship_types=list(relationship_type),
            relation_kinds=list(relation_kind),
            expand_entity_ids=list(expand_entity_id),
            max_documents=max_documents,
        )
        evidence_store = PegaEvidenceStore(configured_cache, project, release)
        async with PegaMCPClient(
            mcp_command, list(mcp_arg), cwd=mcp_cwd, exclude_env={api_key_env}
        ) as client:
            provider, package, evidence_refresh = await prepare_pega_evidence(
                project=project,
                release=release,
                client=client,
                source_root=Path(mcp_cwd),
                store=evidence_store,
                request=request,
            )
            previous_run = (
                None
                if replan or plan_file or resume_existing
                else (incremental_from or evidence_store.latest_run(request))
            )
            docs_path = await write_pages(
                provider,
                package,
                mcp_cwd,
                evidence_refresh,
                previous_run,
                resume_existing,
            )
            evidence_store.record_run(request, output)
            return docs_path

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


@click.command("pega-serve")
@click.option("--project", help="PEGA project ID; defaults to PEGA_PROJECT_ID when set")
@click.option(
    "--run-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Serve this run instead of locating the latest one",
)
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Evidence cache to search; defaults to CODEWIKI_PEGA_CACHE_DIR",
)
@click.option(
    "--runs-root",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Additional directory to search for older runs",
)
@click.option(
    "--bind", "bind_address", default="127.0.0.1", show_default=True,
    help="Local address for the HTTP server",
)
@click.option("--port", default=8766, type=click.IntRange(1, 65535), show_default=True)
def pega_serve_command(
    project: str | None,
    run_dir: Path | None,
    cache_dir: Path | None,
    runs_root: tuple[Path, ...],
    bind_address: str,
    port: int,
) -> None:
    """Serve the latest PEGA wiki locally, without requiring its run directory."""
    from dotenv import load_dotenv

    repo_root = Path(__file__).resolve().parents[3]
    load_dotenv(repo_root / ".env.local", override=False)
    project = project or os.environ.get("PEGA_PROJECT_ID") or None
    output_root = Path(os.environ.get("CODEWIKI_PEGA_OUTPUT_ROOT", "runs")).expanduser()
    if not output_root.is_absolute():
        output_root = repo_root / output_root
    if cache_dir is not None:
        configured_cache = cache_dir.expanduser()
    elif os.environ.get("CODEWIKI_PEGA_CACHE_DIR"):
        configured_cache = Path(os.environ["CODEWIKI_PEGA_CACHE_DIR"]).expanduser()
        if not configured_cache.is_absolute():
            configured_cache = repo_root / configured_cache
    else:
        configured_cache = output_root / ".codewiki-pega-cache"
    if not configured_cache.is_absolute():
        configured_cache = Path.cwd() / configured_cache

    if run_dir is None:
        from codewiki.src.be.pega_refresh import find_latest_pega_run

        search_roots = [repo_root / "runs", Path.cwd() / "runs", output_root]
        search_roots.extend(runs_root)
        run_dir = find_latest_pega_run(
            cache_root=configured_cache,
            search_roots=list(dict.fromkeys(path.resolve() for path in search_roots)),
            project_id=project,
        )
    if run_dir is None:
        qualifier = f" for project {project!r}" if project else ""
        raise click.ClickException(
            f"No completed PEGA CodeWiki run was found{qualifier}. Run `codewiki pega-generate` first."
        )
    run_dir = run_dir.expanduser().resolve()
    manifest_path = run_dir / "evidence-manifest.json"
    required_run_files = (
        run_dir / "evidence-package.json",
        run_dir / "plan.json",
        manifest_path,
        run_dir / "docs" / "module_tree.json",
    )
    if not all(path.is_file() for path in required_run_files):
        raise click.ClickException(f"Not a completed PEGA CodeWiki run: {run_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Cannot read PEGA run manifest at {manifest_path}: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("source_kind") != "pega"
        or (project and manifest.get("project_id") != project)
    ):
        raise click.ClickException(f"Run at {run_dir} does not match the requested PEGA project")

    if not (run_dir / "index.html").is_file():
        from codewiki.cli.pega_viewer import render_pega_viewer

        try:
            render_pega_viewer(run_dir)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise click.ClickException(f"Could not build the run viewer: {exc}") from exc

    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    try:
        server = ThreadingHTTPServer(
            (bind_address, port),
            partial(SimpleHTTPRequestHandler, directory=str(run_dir)),
        )
    except OSError as exc:
        raise click.ClickException(f"Cannot serve on {bind_address}:{port}: {exc}") from exc
    display_host = "127.0.0.1" if bind_address in {"0.0.0.0", "::"} else bind_address
    click.echo(f"Serving PEGA CodeWiki run: {run_dir}")
    click.echo(f"Open http://{display_host}:{port}/index.html (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nStopped PEGA CodeWiki viewer.")
    finally:
        server.server_close()
