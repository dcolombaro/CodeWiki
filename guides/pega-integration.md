# PEGA knowledge base source mode

This fork adds a PEGA source route to CodeWiki. It reads a project-owned Neo4j
knowledge base through the `pega-kb-neo4j` MCP service. XML extraction, rule
selection, semantic enrichment, graph projection, and indexing remain in the
PEGA pipeline. CodeWiki does not parse raw XML during documentation generation.

The current entry points generate **one bounded graph slice**. A whole-project
wiki and production incremental updater are later stages. The first slice
should include its entry rules, dispatcher, one expanded downstream strategy,
and a reviewed module plan. Other branches can remain at the dispatcher
boundary; links to rules shared with the selected strategy may still appear.

## Prerequisites

- Install this fork in Python 3.12 or newer and the PEGA pipeline in its own
  Python 3.11 or newer environment.
- Restore and validate the PEGA Neo4j knowledge base with its owner-provided
  workflow. Keep the knowledge base unchanged during one capture or generation
  run: the MCP contract does not expose an atomic published revision for every
  response.
- Use an explicit customer-approved, OpenAI-compatible model API base URL,
  model ID, and API key for `pega-generate`. Snapshot, planning validation, and
  evidence comparison do not call a CodeWiki model.
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

## Capture official evidence

Set the project and PEGA bundle paths in your shell, then select an exact graph
entity ID for the entry rule. Use `search_entities` through the PEGA MCP service
if the ID is not known; `pega-snapshot` also supports `--seed-name` plus rule,
class, and ruleset filters when that combination resolves uniquely.

```bash
export PEGA_PROJECT_ID='project-id-from-projects-yaml'
export PEGA_KB_ROOT='/absolute/path/to/pega-kb'
export PEGA_PYTHON="$PEGA_KB_ROOT/.venv/bin/python"
export PEGA_SEED_ID='project-prefixed-graph-entity-id'

codewiki pega-snapshot \
  --project "$PEGA_PROJECT_ID" \
  --mcp-command "$PEGA_PYTHON" \
  --mcp-arg=-m --mcp-arg=pega_kb.neo4j_mcp \
  --mcp-cwd "$PEGA_KB_ROOT" \
  --seed-id "$PEGA_SEED_ID" \
  --depth 1 \
  --relationship-type SERVICE_INVOKES_ACTIVITY \
  --relationship-type CALLS_ACTIVITY \
  --relationship-type RUNS_DATA_TRANSFORM \
  --relationship-type EVALUATES_DECISION_TABLE \
  --relationship-type USES_DATA_PAGE \
  --output runs/pega-evidence
```

Use `--expand-entity-id` to extend one entity already selected by the seed
scope by one more edge. Repeat it for multiple selected branches. The named
relationship types control traversal; all typed edges between captured
entities remain in the evidence package. Check the live schema before choosing
types, and narrow the scope if the default 40-document limit is exceeded.

The command checks `list_projects`, `kb_status`, and `describe_graph`, then
saves `evidence-package.json` and exact official Markdown returned by the
graph-selected document IDs. It preserves relationship ID, direction,
qualifiers, source locators, resolution outcomes, and local document hashes.
The `snapshot_key` is an adapter fingerprint of this capture, not an atomic
Neo4j revision. A documented rule becomes a CodeWiki `Node` compatibility
record; its selected behavioral `depends_on` links do not replace the typed
relationship evidence.

## Validate a module plan

Write a JSON plan using the exact `snapshot_key` and documented entity IDs in
the captured package. Give every documented rule one primary module. External
references and embedded entities are supporting context, not module owners.
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
`plan.json`. Its coverage count applies only to the captured slice. If a
reviewed plan is not available, `pega-generate` can ask the configured model to
propose one and applies the same ownership validation.

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
PEGA_SNAPSHOT_DIR='runs/pega-evidence'
PEGA_PLAN_FILE='runs/pega-plan/plan.json'
# For live generation only:
PEGA_KB_ROOT='/absolute/path/to/pega-kb'
PEGA_PYTHON='/absolute/path/to/pega-kb/.venv/bin/python'
PEGA_SEED_ID='project-prefixed-graph-entity-id'
```

The generic helper replays the verified snapshot, uses the reviewed plan when
`PEGA_PLAN_FILE` is set, and writes a fresh wiki run:

```bash
scripts/pega_wiki_demo.sh
# Or: scripts/pega_wiki_demo.sh /path/to/new-run
```

Frozen replay offers only captured evidence; its live retrieval specialist is
disabled. For live retrieval and the bounded specialist, load `.env.local` into
the shell and use the CLI with the same approved model settings:

```bash
set -a
source .env.local
set +a
codewiki pega-generate \
  --project "$PEGA_PROJECT_ID" \
  --mcp-command "$PEGA_PYTHON" \
  --mcp-arg=-m --mcp-arg=pega_kb.neo4j_mcp \
  --mcp-cwd "$PEGA_KB_ROOT" \
  --seed-id "$PEGA_SEED_ID" \
  --depth 1 \
  --model "$CUSTOMER_MODEL_ID" \
  --model-base-url "$CUSTOMER_MODEL_BASE_URL" \
  --api-key-env CUSTOMER_MODEL_API_KEY \
  --output runs/pega-wiki-live
```

Repeat the relationship and branch expansion options used for the reviewed
snapshot when they are relevant. Pass `--plan-file` only if its `snapshot_key`
exactly matches the new capture. The model key is removed from the PEGA MCP
subprocess environment. Both root and recursive CodeWiki writers use the PEGA
evidence tools; their editor read root is the local evidence cache, not the XML
export. The specialist can search, inspect, traverse, and read selected
Markdown under a bounded retrieval budget.

A successful run writes module pages, `overview.md`, the module tree, the plan,
official document copies, directed relationship receipts, and a manifest with
source, model, prompt, timing, and available usage metadata. Each module page
receives a deterministic inventory of its owned rules and outgoing edges. The
writer checks rule ownership, required pages, local citations, and local links.
CodeWiki's remote Mermaid renderer is disabled for this route; diagram source
remains in Markdown. The command does not use the web viewer.

## Review and refresh

Before generation, choose a fixed small set of questions for the selected
slice. Good questions cover entry variants and methods, request validation,
branch conditions, response and error mapping, one downstream data-page route,
and a real unresolved reference. Compare each answer with its official Markdown
or directed edge receipt. Review a sample of substantive claims for support,
inference, contradiction, or missing evidence. Check that the overview links
modules and that every documented rule has one primary owner.

For a refresh, capture the **same** seed, depth, relationship types, and branch
expansions after a controlled upstream knowledge change. Regenerate into a new
empty directory, then compare both evidence packages:

```bash
codewiki pega-compare \
  --project "$PEGA_PROJECT_ID" \
  --before-dir runs/pega-evidence \
  --after-dir runs/pega-evidence-refreshed \
  --before-plan runs/pega-plan/plan.json \
  --output runs/pega-evidence-comparison.json
```

The comparison reports changed document bodies or metadata, entities, directed
edges and qualifiers, and existing module owners affected. It labels a
changed selection as `scope_change`; the same selection is
`evidence_refresh`. Older snapshots without metadata hashes still compare
Markdown hashes and manifest fields; the report records metadata-hash coverage.
A diff is evidence bookkeeping, so review the regenerated
pages and citations to establish that the wiki reflects the change. This is a
full-slice refresh, not a production incremental updater. Upstream corpus
verification is a separate gate from retrieval readiness of a restored dump.
