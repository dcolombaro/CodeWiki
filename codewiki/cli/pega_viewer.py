"""Build a local, navigable viewer for a generated PEGA evidence run."""

from __future__ import annotations

import html
import hashlib
import json
import os
import posixpath
import re
import shutil
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt

from codewiki.src.be.sources.evidence import stable_json


def _page_key(run_dir: Path, path: Path) -> str:
    return path.relative_to(run_dir).as_posix()


def _heading_slug(text: str, used: dict[str, int]) -> str:
    slug = re.sub(r"[^\w\- ]", "", text.casefold(), flags=re.UNICODE)
    slug = re.sub(r"\s+", "-", slug).strip("-") or "section"
    count = used.get(slug, 0)
    used[slug] = count + 1
    return f"{slug}-{count}" if count else slug


def _front_matter(markdown: str) -> tuple[str, str]:
    if not markdown.startswith("---\n"):
        return "", markdown
    end = markdown.find("\n---\n", 4)
    if end < 0:
        return "", markdown
    return markdown[4:end], markdown[end + 5 :]


def _route(key: str, anchor: str = "") -> str:
    return "#/" + quote(key, safe="") + (":" + quote(anchor, safe="") if anchor else "")


def _render_page(
    markdown: str, key: str, available: set[str], parser: MarkdownIt
) -> dict[str, object]:
    front_matter, body = _front_matter(markdown)
    tokens = parser.parse(body)
    used_slugs: dict[str, int] = {}
    headings: list[dict[str, str | int]] = []
    title = key.rsplit("/", 1)[-1].removesuffix(".md").replace("_", " ")

    for index, token in enumerate(tokens):
        if token.type == "heading_open" and index + 1 < len(tokens):
            inline = tokens[index + 1]
            heading_text = "".join(
                child.content for child in (inline.children or []) if child.type in {"text", "code_inline"}
            ).strip() or inline.content.strip()
            anchor = _heading_slug(heading_text, used_slugs)
            token.attrSet("id", anchor)
            level = int(token.tag[1])
            headings.append({"text": heading_text, "anchor": anchor, "level": level})
            if level == 1 and title == key.rsplit("/", 1)[-1].removesuffix(".md").replace("_", " "):
                title = heading_text

        for child in token.children or []:
            if child.type != "link_open":
                continue
            original = child.attrGet("href") or ""
            parts = urlsplit(original)
            if parts.scheme in {"https", "http", "mailto"}:
                child.attrSet("target", "_blank")
                child.attrSet("rel", "noopener noreferrer")
                child.attrSet("referrerpolicy", "no-referrer")
                continue
            if parts.scheme or parts.netloc or original.startswith("/"):
                child.attrSet("href", "#")
                continue
            if not parts.path and parts.fragment:
                child.attrSet("href", _route(key, unquote(parts.fragment)))
                continue
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(key), unquote(parts.path))
            )
            if target in available:
                child.attrSet("href", _route(target, unquote(parts.fragment)))
            else:
                child.attrSet("href", "#")
                child.attrSet("title", "Source is outside this captured wiki")

    rendered = parser.renderer.render(tokens, parser.options, {})
    if front_matter:
        rendered = (
            '<details class="source-metadata"><summary>Source metadata</summary><pre>'
            + html.escape(front_matter)
            + "</pre></details>"
            + rendered
        )
    return {"title": title, "html": rendered, "headings": headings}


def _navigation(
    module_tree: dict,
    pages: dict[str, dict[str, object]],
    source_pages: dict[str, dict[str, str]],
) -> str:
    module_links = [
        '<a class="nav-link" href="' + _route("docs/overview.md") + '">Overview</a>'
    ]
    def add_modules(tree: dict, depth: int = 0) -> None:
        for module, info in tree.items():
            key = f"docs/{module}.md"
            children = info.get("children") or {}
            if children:
                module_links.append('<details class="nav-module" open><summary>')
            if key in pages:
                module_links.append(
                    '<a class="nav-link" style="padding-left:'
                    + str(12 + depth * 14)
                    + 'px" href="'
                    + _route(key)
                    + '">'
                    + html.escape(str(pages[key].get("title") or module.replace("_", " ")))
                    + "</a>"
                )
            if children:
                module_links.append('</summary>')
                add_modules(children, depth + 1)
                module_links.append('</details>')

    add_modules(module_tree)

    source_links = []
    edge_links = []
    for key, page in source_pages.items():
        label = html.escape(page["title"])
        source_links.append(
            '<a class="nav-link" href="' + _route(key) + '" title="' + label + '">' + label + "</a>"
        )
    for key, page in pages.items():
        if not key.startswith("evidence/"):
            continue
        label = html.escape(str(page["title"]))
        item = '<a class="nav-link" href="' + _route(key) + '" title="' + label + '">' + label + "</a>"
        (edge_links if key.startswith("evidence/edges/") else source_links).append(item)

    return (
        '<div class="nav-section documentation-navigation"><div class="nav-heading">Documentation</div>'
        + "".join(module_links)
        + '</div><details class="nav-section evidence-navigation"><summary>Evidence</summary>'
        + '<p class="nav-note">Source documents and relationship receipts supporting the chapter citations.</p>'
        + '<details class="nav-section"><summary>Official sources <span class="count">'
        + str(len(source_links))
        + "</span></summary>"
        + "".join(source_links)
        + '</details><details class="nav-section"><summary>Relationship receipts <span class="count">'
        + str(len(edge_links))
        + "</span></summary>"
        + "".join(edge_links)
        + "</details></details>"
    )


def render_pega_viewer(run_dir: Path) -> Path:
    """Write ``index.html`` for an existing PEGA wiki without model or network calls."""
    run_dir = run_dir.resolve()
    docs_dir = run_dir / "docs"
    overview = docs_dir / "overview.md"
    module_tree_path = docs_dir / "module_tree.json"
    if not overview.is_file() or not module_tree_path.is_file():
        raise ValueError("Run must contain docs/overview.md and docs/module_tree.json")

    evidence_manifest_path = run_dir / "evidence-manifest.json"
    if evidence_manifest_path.is_file():
        evidence_manifest = json.loads(evidence_manifest_path.read_text(encoding="utf-8"))
        for document_id, record in (evidence_manifest.get("documents") or {}).items():
            cache_path = record.get("cache_path") if isinstance(record, dict) else None
            if not isinstance(cache_path, str) or not cache_path.startswith("evidence/"):
                raise ValueError(f"Invalid cached evidence path for {document_id}")
            source_link = run_dir / cache_path
            try:
                Path(os.path.abspath(source_link)).relative_to(
                    Path(os.path.abspath(run_dir / "evidence"))
                )
            except ValueError as exc:
                raise ValueError(f"Evidence path escapes run for {document_id}") from exc
            source_path = source_link.resolve()
            if not source_path.is_file():
                raise FileNotFoundError(f"Missing official Markdown for {document_id}: {source_path}")
            actual_hash = hashlib.sha256(
                source_path.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest()
            if actual_hash != record.get("sha256"):
                raise ValueError(f"Official Markdown changed since capture: {document_id}")
            metadata_path = source_path.with_suffix(".json")
            if metadata_path.is_file() and record.get("metadata_sha256"):
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if hashlib.sha256(stable_json(metadata).encode("utf-8")).hexdigest() != record["metadata_sha256"]:
                    raise ValueError(f"Official document metadata changed since capture: {document_id}")

    docs_paths = sorted(docs_dir.glob("*.md"))
    evidence_dir = run_dir / "evidence"
    evidence_paths: list[Path] = []
    if evidence_dir.is_dir():
        evidence_paths = sorted(evidence_dir.rglob("*.md"))
    source_records = {
        record.get("cache_path"): record
        for record in (evidence_manifest.get("documents") or {}).values()
        if isinstance(record, dict)
    } if evidence_manifest_path.is_file() else {}
    source_paths = [path for path in evidence_paths if "edges" not in path.relative_to(evidence_dir).parts]
    edge_paths = [path for path in evidence_paths if "edges" in path.relative_to(evidence_dir).parts]
    source_pages: dict[str, dict[str, str]] = {}
    for path in source_paths:
        key = _page_key(run_dir, path)
        sidecar = path.with_suffix(".json")
        metadata = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
        record = source_records.get(key)
        source_hash = record.get("sha256") if record else None
        if not source_hash:
            source_hash = hashlib.sha256(
                path.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest()
        source_pages[key] = {
            "title": str(metadata.get("title") or path.stem),
            "sha256": str(source_hash),
        }
    paths = docs_paths + edge_paths
    available = {_page_key(run_dir, path) for path in docs_paths + evidence_paths}

    parser = MarkdownIt("commonmark", {"html": False, "linkify": False})
    parser.enable("table")

    def text_only_image(tokens, idx, options, env):
        return '<span class="image-note">[Image: ' + html.escape(tokens[idx].content) + "]</span>"

    parser.add_render_rule("image", text_only_image)
    pages = {
        _page_key(run_dir, path): _render_page(
            path.read_text(encoding="utf-8"), _page_key(run_dir, path), available, parser
        )
        for path in paths
    }
    module_tree = json.loads(module_tree_path.read_text(encoding="utf-8"))
    metadata_path = docs_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    project = metadata.get("generation_info", {}).get("pega_project_id") or "PEGA"
    scope_note = "This wiki covers a selected PEGA graph slice."
    package_path = run_dir / "evidence-package.json"
    if package_path.is_file():
        package = json.loads(package_path.read_text(encoding="utf-8"))
        scope = package.get("scope", {})
        if scope.get("mode") == "project":
            scope_note = "This wiki covers the captured PEGA project knowledge base."
        scope_note += (
            f" Captured scope: {scope.get('document_count', 0)} official documents, "
            f"{scope.get('entity_count', 0)} graph entities, "
            f"{len(package.get('relationships', []))} directed relationships."
        )
    template_path = Path(__file__).parent.parent / "templates" / "pega_viewer" / "viewer_template.html"
    template = template_path.read_text(encoding="utf-8")
    # Escaping '<' keeps Markdown content from closing the inline JSON script.
    pages_json = json.dumps(pages, ensure_ascii=False).replace("<", "\\u003c")
    rendered = (
        template.replace("{{TITLE}}", html.escape(f"{project} | PEGA CodeWiki"))
        .replace("{{PROJECT}}", html.escape(project))
        .replace("{{SCOPE_NOTE}}", html.escape(scope_note))
        .replace("{{NAVIGATION}}", _navigation(module_tree, pages, source_pages))
        .replace("{{PAGES_JSON}}", pages_json)
        .replace("{{SOURCES_JSON}}", json.dumps(source_pages, ensure_ascii=False).replace("<", "\\u003c"))
    )
    vendor_dir = template_path.parent / "vendor"
    asset_dir = run_dir / "assets"
    asset_dir.mkdir(exist_ok=True)
    for filename in ("mermaid-11.9.0.min.js", "MERMAID-LICENSE.txt"):
        shutil.copyfile(vendor_dir / filename, asset_dir / filename)

    output = run_dir / "index.html"
    temporary = run_dir / ".index.html.tmp"
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, output)
    return output
