# PEGA knowledge base source mode

This fork adds a PEGA source route to CodeWiki. It reads a project-owned Neo4j
knowledge base through the `pega_kb.mcp_server` MCP service. XML extraction, rule
selection, semantic enrichment, graph projection, and indexing remain in the
PEGA pipeline. CodeWiki does not parse raw XML during documentation generation.

The default PEGA entry point captures the **entire selected project**. It
enumerates every graph entity and directed PEGA-to-PEGA relationship, then
loads all official Markdown linked to those entities. A seed and depth are
optional controls for a smaller investigation such as the CercaAgenzia PoC.
Unattended refresh scheduling remains a separate operational choice.

## Prerequisites

- Install this fork in Python 3.12 or newer and the PEGA pipeline in its own
  Python 3.11 or newer environment.
- Restore and validate the PEGA Neo4j knowledge base with its owner-provided
  workflow. CodeWiki reads the existing project manifest from Neo4j through
  the PEGA MCP `read_cypher` tool, falling back to existing row projection
  tokens for older KBs. No PEGA Agent output-contract change is required.
- Use an explicit customer-approved, OpenAI-compatible model API base URL,
  model ID, and API key for `pega-generate`.
- Use a new, empty output directory for each run.

Install the fork with `python -m pip install -e .` in a Python 3.12+
environment. When using an existing installation of CodeWiki, put this fork
first on `PYTHONPATH` before running the commands:

```bash
cd /path/to/CodeWiki
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
```

Keep project IDs, graph IDs, credentials, and saved evidence outside versioned
files when the CodeWiki fork is public. The `runs/` directory and `.env.local`
are Git-ignored.

## Manual evidence snapshot for inspection or replay

Normal documentation generation does not require a separate snapshot command.
`pega-generate` checks the current project revision, reuses a matching cached
package, or captures the requested scope when the cache is stale or incomplete.
Use `pega-snapshot` only when you specifically want to inspect, archive, or
replay an evidence package independently of generation.

Set the project, release, and new bundle paths in your shell. For this UnipolLead
bundle, the default release is `lead`; `leadtest` is a separate, duplicated
application release and must be generated as a separate wiki.

```bash
export PEGA_PROJECT_ID='unipolLead'
export PEGA_RELEASE='lead'
export PEGA_KB_ROOT='/absolute/path/to/pega-kb-codex-GTLLife-bundle'
export PEGA_PYTHON="$PEGA_KB_ROOT/.venv/bin/python"

codewiki pega-snapshot \
  --project "$PEGA_PROJECT_ID" --release "$PEGA_RELEASE" \
  --mcp-command "$PEGA_PYTHON" \
  --mcp-arg=-m --mcp-arg=pega_kb.mcp_server \
  --mcp-cwd "$PEGA_KB_ROOT" \
  --output runs/pega-evidence
```

For a focused slice, add `--seed-id ID` or `--seed-name NAME` and `--depth N`.
The optional `--relationship-type` values select Neo4j edge types such as
`CALLS`; `--relation-kind` selects PEGA meanings stored on those edges, such as
`CALLS_ACTIVITY` or `RUNS_DATA_TRANSFORM`. Either filter controls which edges
expand the slice and which matching edges are retained in its focused evidence
package. `--expand-entity-id` extends a selected branch by one hop using the
same filters. `--traversal-direction outgoing` follows dependencies from the
seed in their configured direction, while the default `both` explores either
end of a relationship. Focused depth can be 1–16. The 40-document default
limit applies only to focused slices; increase `--max-documents` for a larger
Case Type process when the evidence justifies it.

A focused seed whose rule type is `Rule-Obj-CaseType` activates the process
planner. It reads the Case Type's official Markdown to obtain the ordered
primary and alternate stages, matches each stage process to its captured
`STARTS_FLOW` relationship, then follows directed `CALLS`, `READS`, and
`WRITES` dependencies within the evidence package. The tree has one process
overview with stage chapters in source order. Large stages can have Flow
journey and supporting subchapters. Rules reached equally from multiple
stages receive one shared chapter; every documented rule still has exactly
one primary leaf owner. Page writers read the selected Markdown and edge
receipts through the same evidence tools as other PEGA runs. A seed that is
not a Case Type keeps the existing focused planner. A complete release with
`--doc-type functional` automatically anchors documented Case Types as process
parents and places rules reused across cases in a shared area. Rules outside
those case journeys remain in an additional capability area. A release with
no documented Case Types, such as the captured `unipolLead` / `lead` input,
keeps the existing project capability planner.
The process planner requires the Case Type's stage/process configuration and
the referenced Flow rules with official Markdown; it reports incomplete
evidence instead of inventing a stage hierarchy.
The [GTLLife iLove example](pega-case-type-processes.md) gives a concrete
Case Type command and shows how to inspect its process hierarchy.

The adapter checks `catalog`, `list_releases`, and `kb_status`, then binds the
capture to exactly one release. It pages the release's `Scoped` nodes and
directed edges with `read_cypher`, retaining each node's full properties and
each edge's full properties, type, and direction. `markdown_path` on a node
selects its deterministic Markdown file; CodeWiki verifies the file SHA-256
and keeps a link to the source rather than copying the document. A release is
accepted only when node, edge, and Markdown-reference counts agree with the
release manifest. `content_hash` covers the graph projection; refresh revision
also hashes every referenced Markdown file so deterministic Markdown changes
invalidate cached evidence. The `snapshot_key` fingerprints the captured
release package. A
documented rule becomes a CodeWiki `Node` compatibility record; its selected
behavioral `depends_on` links do not replace the typed relationship evidence.

## Validate a module plan

Write a JSON plan using the exact `snapshot_key` and documented entity IDs in
the captured package. Give every documented rule one primary module. An entity
without a PEGA rule type or selected official Markdown remains supporting
context. External or embedded rules with their own selected Markdown can own a
module page.
A small plan has this shape:

```json
{
  "snapshot_key": "key-from-evidence-package",
  "modules": [
    {
      "name": "Entry_Points",
      "purpose": "Configured entry rules and their directed links",
      "entity_ids": ["project-prefixed-entity-id"]
    },
    {
      "name": "Dispatch_and_Strategy",
      "purpose": "Dispatcher and selected downstream rules",
      "entity_ids": ["another-project-prefixed-entity-id"]
    }
  ]
}
```

```bash
codewiki pega-plan \
  --project "$PEGA_PROJECT_ID" \
  --snapshot-dir runs/pega-evidence \
  --plan-file /path/to/reviewed-plan.json \
  --output runs/pega-plan
```

`pega-plan` verifies the snapshot key, cached Markdown hashes, graph references,
and exact rule ownership without contacting Neo4j or a model. It writes
`components.json`, `module_tree.json`, `validation.json`, and a reusable
`plan.json`. Its coverage count applies to the scope recorded in the package.
If a reviewed plan is unavailable, `pega-generate` discovers provisional
capabilities in batches, then reconciles them across the complete project.
The global pass sees all rule identities, evidence-derived candidate purposes,
and summaries of every directed relationship. It may merge or split candidates
and move rules across batches to keep complete workflows together. It produces
capability overview pages with coherent leaf chapters. Every documented rule
has one primary leaf owner; parent components roll up their descendants.
`plan.json` preserves planning provenance and recursive `modules`, while
`module_tree.json` supplies the hierarchy used by writers. Focused slices keep
a planner that can consider their complete selected context in one call.

Review leaf memberships as well as coverage: endpoint handlers, contracts, and
validation should stay with their workflow, while declaration-only fragments
belong in a relevant capability. A reviewed `--plan-file` can contain recursive
`children` for parents and `entity_ids` only on leaves. Parent `components` are
derived from their descendants. Record deliberate regroupings in
`planning.editorial_adjustments`; generation preserves this provenance.

## Configure the model and generate pages

Copy the local template, restrict its permissions, and fill the approved model
settings. Do not commit or paste the key into chat:

```bash
cp .env.local.example .env.local
chmod 600 .env.local
```

```bash
# In .env.local:
CUSTOMER_MODEL_API_KEY='customer-provided-secret'
CUSTOMER_MODEL_BASE_URL='https://customer-approved-provider.example/v1'
CUSTOMER_MODEL_ID='customer-approved-model-id'
PEGA_PROJECT_ID='project-id-from-projects-yaml'
# Optional reviewed plan for the exact current evidence package.
PEGA_PLAN_FILE=''
# Required for the automatic revision check and evidence refresh:
PEGA_RELEASE='lead'
PEGA_KB_ROOT='/absolute/path/to/pega-kb-codex-GTLLife-bundle'
PEGA_PYTHON='/absolute/path/to/pega-kb-codex-GTLLife-bundle/.venv/bin/python'
# Optional persistent cache location; defaults below the output root.
CODEWIKI_PEGA_CACHE_DIR='runs/.codewiki-pega-cache'
# Optional focused request; leave the seed empty for the whole project.
# PEGA_SEED_ID='project-prefixed-graph-entity-id'
# Optional: select functional documentation and a reader audience.
PEGA_DOC_TYPE='functional'
PEGA_INSTRUCTIONS='Write for business analysts.'
```

The helper checks the live revision, uses the reviewed plan when
`PEGA_PLAN_FILE` is set, and writes a new wiki run:

```bash
scripts/pega_wiki_demo.sh
# Or: scripts/pega_wiki_demo.sh /path/to/new-run
```

Every run generates only from one verified evidence package. The automatic
CLI workflow reads the selected release manifest revision (or row-token fallback)
first. If the cached package matches, it skips graph/document capture and
reuses the cached component view; if not, it captures the requested project
or slice and checks the revision again before saving it. A complete cached
project package can also supply a focused slice locally. CodeWiki then
disconnects the MCP transport before planning and writing. To run it directly:

```bash
set -a
source .env.local
set +a
codewiki pega-generate \
  --project "$PEGA_PROJECT_ID" \
  --release "$PEGA_RELEASE" \
  --mcp-command "$PEGA_PYTHON" \
  --mcp-arg=-m --mcp-arg=pega_kb.mcp_server \
  --mcp-cwd "$PEGA_KB_ROOT" \
  --model "$CUSTOMER_MODEL_ID" \
  --model-base-url "$CUSTOMER_MODEL_BASE_URL" \
  --api-key-env CUSTOMER_MODEL_API_KEY \
  --doc-type functional \
  --instructions "Write for business analysts" \
  --output runs/pega-wiki-live
```

`functional` organizes the wiki around configured business capabilities,
conditions, decisions, outcomes, and exceptions. Routine Pega step numbers,
clipboard fields, class names, and rule inventories are summarized in the
main narrative; exact source traceability remains in citations and inventories.
The other PEGA types are `api`, `architecture`, `user-guide`, and `developer`.
`user-guide` is for documented user tasks, so it should not be used to infer
screens or instructions absent from the KB. The `--instructions` option can
name a more specific reader audience. Both controls reach discovery,
project-wide reconciliation, leaf writers, and parent overviews. A style change
forces a new plan and pages while reusing compatible captured evidence.

Add seed, depth, relationship, and branch expansion options only for a focused
slice. Matching previous wiki pages are reused automatically. Use `--replan`
to reuse the package while rerunning planning and every writer. `--snapshot-dir`
remains an explicit offline replay option for diagnostics and reproducibility.
The model key is removed from the PEGA MCP
subprocess environment. CodeWiki's leaf writers and parent overviews follow the
validated hierarchy; writers cannot add pages or change ownership. Writers use the PEGA
evidence tools; official source reads go through the bounded evidence reader,
not the XML export or the generic repository editor. During generation, the
specialist can search, inspect, traverse, and read package-selected Markdown
under a bounded retrieval budget; it cannot enlarge the captured graph or
document set.

If a live writer process is interrupted, resume into the same output directory
with the same project, release, model, and MCP options, adding
`--resume-existing`. CodeWiki rechecks the live input revision, verifies that
the saved evidence package and plan still match it, and validates the module
tree. It then keeps completed pages and writes only missing pages. If the graph
or Markdown changed, or the plan/tree no longer matches, resume stops. Start a
normal fresh run in a new output directory instead. Resume cannot be combined
with snapshot replay, incremental-from, a supplied plan, replan, or bundled
evidence.

A successful run writes module pages, `overview.md`, the module tree, the plan,
directed relationship receipts, and a manifest with source, model, prompt,
timing, and available usage metadata. `evidence/` holds links to the original
PEGA Agent Markdown plus small JSON metadata records. A fresh capture reads
each selected file through MCP and compares its content hash with the local
source before linking it; subsequent matching runs reuse those hashes and
links. The shared evidence cache and generated runs keep links and metadata
rather than Markdown copies. Each leaf receives a deterministic inventory
of its owned rules and outgoing edges. For `--doc-type functional`, the inventory
is a linked page under `evidence/inventories/`; other styles append it to the
leaf chapter. The
manifest records evidence storage mode, evidence section reads, and returned
hashes. The writer checks rule ownership, required pages, local citations, and
local links.
CodeWiki's remote Mermaid renderer is disabled for this route; diagram source
remains in Markdown. The command also writes `index.html` at the run root. This
local viewer renders the module pages, official documents, and directed
relationship receipts as a navigable wiki. It bundles the rendered content in
the HTML file. The Documentation navigation follows the capability hierarchy;
official sources, relationship receipts, and functional module inventories are
grouped in a separate, collapsed Evidence section. Receipts support citations
and are not module chapters.
The viewer copies a pinned Mermaid browser bundle into `assets/` to render
diagrams locally. Official Markdown is fetched from `evidence/` only when its
viewer page is opened, then checked against the captured hash. The viewer makes
no CDN or rendering-service requests. To inspect a run's linked `evidence/`
files, keep the shared cache and PEGA Agent source tree available at their
recorded paths. Pass `--bundle-evidence` when the run must move independently
of the cache and source tree; that option intentionally copies selected
Markdown and metadata into the run.

To serve the latest run for `PEGA_PROJECT_ID`, without looking up its run
directory:

```bash
codewiki pega-serve
```

It prints the URL and serves the latest completed run on port 8766. Use
`--port 8767` if 8766 is occupied. To serve a specific earlier run, pass
`--run-dir PATH`. Keep the PEGA Agent source tree available for linked Markdown.
Stop the server with Ctrl-C.

## Review and refresh

Before generation, choose review questions for the intended scope. For an
agency lookup slice, good questions cover entry variants and methods, request validation,
branch conditions, response and error mapping, one downstream data-page route,
and a real unresolved reference. Compare each answer with its official Markdown
or directed edge receipt. Review a sample of substantive claims for support,
inference, contradiction, or missing evidence. Check that the overview links
modules and that every documented rule has one primary owner.

For a whole-project or focused refresh, run the same `pega-generate` command
and scope again. CodeWiki checks the current project revision, refreshes
evidence only when required, and finds the prior wiki for that exact scope
automatically. Generate into a new empty directory:

```bash
codewiki pega-generate \
  --project "$PEGA_PROJECT_ID" \
  --release "$PEGA_RELEASE" \
  --mcp-command "$PEGA_PYTHON" \
  --mcp-arg=-m --mcp-arg=pega_kb.mcp_server \
  --mcp-cwd "$PEGA_KB_ROOT" \
  --model "$CUSTOMER_MODEL_ID" \
  --model-base-url "$CUSTOMER_MODEL_BASE_URL" \
  --api-key-env CUSTOMER_MODEL_API_KEY \
  --output runs/pega-wiki-refreshed
```

The refresh compares graph entities, typed relationships, document hashes and
document metadata when the revision check requires a new capture. With the
same captured selection, documented rules,
validated ownership, writer implementation, prompt, and model settings, it
reuses unaffected module pages, regenerates affected top-level modules and
their nested pages, and rebuilds the overview. The new run links its Markdown
evidence to the new snapshot. It falls back to a full generation if scope,
documented rule ownership, prompt, implementation, or model settings changed,
or if the evidence diff cannot be routed to existing modules. The result
records the comparison, reused pages, regenerated modules, and fallback reason
in `evidence-manifest.json`.
Runs generated by an earlier writer implementation fail the implementation
identity check and trigger a one-time full generation; later refreshes can
reuse pages from that regenerated baseline.

The evidence cache lives under `CODEWIKI_PEGA_CACHE_DIR` (default
`runs/.codewiki-pega-cache`). Use `--replan` to reuse the current package but
rerun planning and all writers; the chat tool accepts `replan=true`. To inspect
Each refresh records evidence changes, affected modules, reused pages, and any
fallback reason in the run `evidence-manifest.json`. Review regenerated pages
and citations to establish that the wiki reflects those changes. Upstream
corpus verification remains a separate gate from retrieval readiness of a
restored dump.
