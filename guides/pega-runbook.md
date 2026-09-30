# PEGA CodeWiki runbook

This runbook shows how to inspect the complete UnipolLead wiki and the earlier
Agency Lookup PoC, their structured dependency evidence, and their module
plans. It traces how CodeWiki turns the PEGA Agent knowledge base (KB) into
final documentation and compares that route with CodeWiki's usual source-code
route.

The full-project run is
`runs/unipollead-project-wiki-terra-20260929/`; its captured graph and
document manifest are in `runs/unipollead-project-evidence-20260929/`. The live MCP capture selected
807 PEGA entities, 1,996 directed relationships, and 493 official Markdown
documents. CodeWiki assigned all 469 documented rules to 40 modules, wrote
40 module pages and an overview, and created a local HTML viewer. The run
directories are Git-ignored. Keep the evidence snapshot beside the wiki and the
PEGA Agent input tree at its recorded path: the run links directly to the
original Markdown files and keeps only metadata and hashes in the evidence
cache.

## View the complete UnipolLead wiki

Serve the latest PEGA run for the configured project on port 8766:

    codewiki pega-serve

The command finds the latest completed run automatically and prints its URL.
Open <http://127.0.0.1:8766/index.html>. The sidebar lists the 40 project
modules; the overview links to every module. The locally bundled Mermaid
script renders diagrams without an external validation or rendering endpoint.
For the exact ownership map, open `docs/module_tree.json` and `plan.json` in
this run. For the captured input graph, open `evidence-package.json`; its
source document bodies are linked from the original PEGA Agent input tree. The
`evidence-manifest.json` records hashes, model calls, and the snapshot link.

## Inspect the earlier Agency Lookup PoC

The example run is runs/cercaagenzia-wiki-terra-frozen-20260928-v3/ (relative
to the CodeWiki repository root). This run directory is Git-ignored. It is one
bounded slice of project unipolLead, not documentation of the whole project
KB. The captured evidence contains 23 graph entities, 35 directed
relationships, and 20 official Markdown documents. Twenty documented rules
have one primary owner in one of four modules; the remaining entities are
supporting context.

## View the generated wiki and dependency diagrams

The run contains index.html and a local Mermaid JavaScript bundle. Serve this
specific historical run with:

    codewiki pega-serve \
      --run-dir runs/cercaagenzia-wiki-terra-frozen-20260928-v3 \
      --port 8767

The command prints the URL; stop the server with Ctrl-C. Use port 8767 when
8766 is already serving the latest project run.

The left navigation links Overview and each module page. Overview has the
cross-module Mermaid diagram; module pages have detailed configured
relationships, including step paths and branches. The sidebar also has
expandable lists of official Markdown sources and graph-relationship
receipts. Diagrams are rendered locally using assets/mermaid-11.9.0.min.js;
official Markdown is fetched from evidence/ when selected and checked against
its captured hash. Keep the original PEGA Agent input tree available at the
paths recorded in the manifest. Frozen runs also link metadata from the source
snapshot, which must remain at the same relative location as the wiki run. The
viewer does not require a CDN or an external rendering endpoint.

Rebuild the HTML viewer for an existing run without another model call:

    codewiki pega-viewer --run-dir runs/cercaagenzia-wiki-terra-frozen-20260928-v3

A successful `pega-generate` run also creates `index.html`; `pega-serve` builds
the viewer automatically when an older run is missing it.

## Inspect the dependency-graph JSON and PEGA evidence

### Normal CodeWiki codebase output

In codebase mode, CodeWiki writes a dependency graph JSON under
OUTPUT_DIR/temp/dependency_graphs/. For example, the LiteCLI run has:

    /home/dcolombaro/projects/Genertel/codewiki_test/artifacts/litecli-docs/temp/dependency_graphs/litecli_dependency_graph.json

This is a JSON object keyed by CodeWiki component ID. Each value is a
component record with fields such as id, name, component_type, file_path,
relative_path, source_code, and depends_on. A depends_on entry is a directed
dependency from the keyed component to the listed target component. The
LiteCLI example contains 201 component records and 253 depends_on links.
For example, litecli/clibuffer.py::cli_is_multiline depends on
litecli/clibuffer.py::_multiline_exception.

To inspect the file structure:

    python -m json.tool /home/dcolombaro/projects/Genertel/codewiki_test/artifacts/litecli-docs/temp/dependency_graphs/litecli_dependency_graph.json | less

To print the source and target of every dependency:

    python - <<'PY'
    import json
    from pathlib import Path

    path = Path('/home/dcolombaro/projects/Genertel/codewiki_test/artifacts/litecli-docs/temp/dependency_graphs/litecli_dependency_graph.json')
    graph = json.loads(path.read_text(encoding='utf-8'))
    print(f'{len(graph)} components')
    for source_id, node in graph.items():
        for target_id in node.get('depends_on') or []:
            print(f'{source_id} -> {target_id}')
    PY

This is CodeWiki's source-code analysis artifact:
[DependencyGraphBuilder](../codewiki/src/be/dependency_analyzer/dependency_graphs_builder.py)
serializes the parsed repository components and their dependencies. It is
machine-readable evidence; the normal CodeWiki viewer's overview and module
pages provide the human-facing diagrams and explanations.

### PEGA source-mode output

The PEGA route does not create
OUTPUT_DIR/temp/dependency_graphs/*_dependency_graph.json. When
source_kind=pega, DocumentationGenerator does not construct a
DependencyGraphBuilder. PegaDocumentationGenerator instead builds an
in-memory compatibility component map from the captured Pega rules and then
uses the Pega-specific planner and writers.

For PEGA, inspect the corresponding machine-readable evidence in
runs/cercaagenzia-wiki-terra-frozen-20260928-v3/evidence-package.json. Its
shape differs from the codebase graph JSON:

- entities is an array of captured graph entity records;
- relationships is an array of typed, directed records, with source and target
  entity IDs, relationship type, supporting document ID, and edge properties;
- documents and scope identify selected Markdown and the bounded capture.

The package retains Pega relationship types and qualifiers such as step_path,
condition, http_method, and resolution_outcome. CodeWiki's in-memory
depends_on compatibility view is derived only from selected behavioral edge
types; it is not a replacement for the typed relationship array. The separate
pega-plan command can write a components.json compatibility view, but the
frozen wiki run does not contain that file. In the temporary CodeWiki Node
view, source_code contains a serialized Pega entity card and file_path points
to cached official Markdown; it is not parsed XML or a source-code file.

To print captured directed relationships from this run:

    python - <<'PY'
    import json
    from pathlib import Path

    path = Path('/home/dcolombaro/projects/Genertel/codewiki-pega/runs/cercaagenzia-wiki-terra-frozen-20260928-v3/evidence-package.json')
    package = json.loads(path.read_text(encoding='utf-8'))
    print(f"{len(package['entities'])} entities, {len(package['relationships'])} relationships")
    for edge in package['relationships']:
        print(
            f"{edge['source_entity_id']} -[{edge['relationship_type']}]-> "
            f"{edge['target_entity_id']} {edge['properties']}"
        )
    PY

For this PoC the package contains 23 entities, 35 relationships, and 20
official documents. Each generated module page also includes an inventory of
owned rules and outgoing edge receipts. The overview and module Mermaid
diagrams are the rendered documentation view; evidence-package.json and the
receipts are the underlying structured evidence.

## View the module tree

The final tree is
runs/cercaagenzia-wiki-terra-frozen-20260928-v3/docs/module_tree.json; the
initial tree is runs/cercaagenzia-wiki-terra-frozen-20260928-v3/docs/first_module_tree.json.
Both are also copied under raw-docs/.

For this run, the two JSON files are byte-for-byte identical. They define four
top-level modules and no nested children:

- Agency_Lookup_API_Entry
- Agency_Lookup_Dispatch_and_Validation
- Agency_Lookup_Other_Selection_Actions
- Agency_Lookup_Geo_Merit_Strategy

The viewer navigation lists module pages, but does not show every tree field.
Read module_tree.json to inspect the hierarchy and rule IDs, or format it in
the terminal:

    python -m json.tool docs/module_tree.json

The run-root plan.json has the rule ownership, module purposes, scope, and
supporting-entity rationale. Each documented rule has exactly one primary
owner, though another page may discuss or link to it.

In a later run, a large module can be split recursively. The final
module_tree.json may then have nested children while first_module_tree.json
preserves the initial plan; do not assume they always match.

## From the PEGA KB to the final wiki

The PEGA Agent prepares the KB. CodeWiki consumes its published graph and
graph-selected official Markdown. CodeWiki does not parse raw Pega XML or
repeat upstream semantic synthesis.

### Upstream KB preparation

The configured PEGA KB workflow is:

    ingest -> synthesize -> verify-corpus -> project-graph -> index

Ingest reads eligible Pega XML exports and produces category-aware official
Markdown. Synthesis can add a functional interpretation to that Markdown.
Deterministic extraction and graph projection remain distinguishable from
LLM-written interpretation. Corpus verification checks the prepared material
before graph projection and indexing publish the Neo4j representation. The
official corpus for a project is under
projects/PROJECT_ID/kb/markdown/synthesis/.

This upstream work is complete before CodeWiki starts. Neo4j is the discovery
source for entity identity and relationships; official Markdown supplies
configuration details not modeled in the graph, such as activity step order,
conditions, and request/response mappings.

### CodeWiki capture and evidence checks

1. **Resolve the project revision and requested scope.** The provider checks
   `kb_status` for project identity and graph counts, then uses the existing
   PEGA MCP `read_cypher` tool to read `KBProject.projection_manifest_hash`
   and its status. If that node is unavailable in an older KB, it derives a
   revision from existing entity `projection_token` values. It stops if the
   available status says indexing is not ready or no stable revision can be
   established. The default scope is the complete project; a focused request
   adds an exact seed and traversal parameters. The persistent cache key
   includes both project and scope.
2. **Reuse or capture graph evidence.** If the cache contains a complete package
   for this scope at the live project revision, CodeWiki reuses it. If a complete
   project package is cached, CodeWiki can derive a requested focused slice
   locally. Otherwise it captures graph evidence. The project capture paginates an empty-query
   `search_entities` inventory including external references, then runs a
   fixed, project-scoped `read_cypher` query for every directed PEGA-to-PEGA
   edge in stable relationship-ID order. It verifies entity, edge, and linked
   document counts. The PoC instead uses `get_entity`: each call returns one
   exact entity and its incoming/outgoing edges, retaining direction and
   properties. A breadth-first walk follows requested edge types up to the
   chosen depth, with one extra hop from an explicitly expanded entity. For
   the historical CercaAgenzia PoC, the selected types cover service
   invocation, activity call, data transform, decision table, and data page;
   the geo-merit strategy is expanded once.
3. **Select and reference official Markdown when capturing.** CodeWiki obtains document IDs
   from the selected entities and reads those documents using MCP
   `get_document`. It reads the local file named by `source_path` and checks
   its hash against the MCP body. The verified local text is kept in memory
   for planning and writing; the cache gets a symlink to the original file and
   a small metadata record with the SHA-256. Generated wiki runs link to the
   same original files; they do not make Markdown copies. The writer receives
   Markdown through the bounded evidence reader and does not open raw XML.
   CodeWiki does not scan the entire Markdown corpus to discover rules.
4. **Bind planning and writing to the package.** Whether reused, derived, or
   freshly captured, `evidence-package.json` contains the exact scope, entity
   cards, typed directed relationships, selected-document manifest, unresolved
   references, and fingerprint used for this run. On a cache hit CodeWiki
   verifies its package metadata and local source hashes without calling
   `get_entity` or `get_document`. A new capture compares each selected source
   hash with the MCP document body. CodeWiki checks the project revision again
   after capture and stops if it changed while evidence was being gathered.
   The manifest records the revision decision as reused, locally derived, or
   recaptured. After this boundary, the specialist only searches and traverses
   the package in memory; it does not query Neo4j.

The historical PoC run records `retrieval_mode: frozen_snapshot`; that label
describes its earlier replay path. New runs use `retrieval_mode:
automatic_evidence_cache`. The immutable package remains an internal run
boundary and can still be replayed manually with `--snapshot-dir` for
diagnostics.

### CodeWiki PEGA adaptation and writing

5. **Adapt PEGA rules to CodeWiki components.** A rule becomes a CodeWiki
   component when it has a PEGA rule type and at least one official Markdown
   document in this evidence package. `is_external` and `is_embedded` remain
   visible as provenance/context flags, but do not disqualify a documented
   rule: the documentation should follow the available evidence rather than
   infer exclusion from those labels. Entities without a rule type or linked
   document remain supporting context and do not own pages. Each component
   keeps the exact graph ID and an official-document link. For compatibility
   with CodeWiki's shared module machinery, a generic `depends_on` view is
   derived from a selected subset of behavioral edge types. This compatibility
   view is not the source of truth: `evidence-package.json` retains all captured
   typed edges and qualifiers.
6. **Plan modules and enforce ownership.** This step builds the page layout;
   it does not expand or rewrite the captured graph. A reviewed plan, when
   supplied, is checked against this exact evidence package. Otherwise the
   planner groups the package's documented rules using each rule's name,
   type, Applies-To class, ruleset, complete selected Markdown section, and graph context. For every
   batch rule, the planner receives all captured directed edges touching that
   rule, including edges to entities outside the batch, plus compact metadata
   cards for those connected entities. This can expose shared dependencies and
   links across batch boundaries. Supporting cards are context only: only
   documented rule IDs in the batch's owner cards can be assigned to modules.
   It prefers the PEGA Agent's
   *Functional synthesis* section when available; that text is an upstream
   interpretation. If it is absent,
   the planner can use *Extracted configuration* instead. It includes the
   complete chosen section, without cutting it to a character count. For a
   whole project, CodeWiki keeps ruleset and Applies-To class groups intact and
   packs them toward the configured planner prompt-size target. It never cuts
   a Markdown section or splits a group to meet that target; an oversized group
   is sent whole. The batch rule counts are recorded in `plan.json`, with no
   fixed rule-count cap. In the final `plan.json` and
   `docs/module_tree.json`, each documented rule ID must have exactly one
   primary module owner. A supporting entity can inform a page without owning
   a page of its own. Invalid model ownership is recorded and assigned to an
   explicit fallback module for that batch; the run manifest exposes any such
   fallback for review. The current planning batch count and any fallback
   batches are recorded in `plan.json`.
7. **Write evidence-linked module pages.** For a leaf in
   `docs/module_tree.json`, its `components` list tells CodeWiki which PEGA
   rules the page is responsible for explaining. CodeWiki gives the writer
   compact cards for those rules and the captured graph relationships that
   touch them. The writer then asks `read_pega_evidence` for selected sections
   from the official Markdown linked to those rules. That tool returns only
   the requested sections with line numbers; it does not send the entire
   Markdown corpus in every prompt. CodeWiki records a hash of each returned
   excerpt in the run manifest. The writer uses those sections to describe
   details such as activity step order, conditions, and mappings, and cites
   the evidence in the page. During generation, a bounded specialist can
   search, inspect, and traverse the captured graph when an
   identity or cross-rule question needs more context. Those operations are
   bounded to the package's entity and relationship IDs. Markdown reads are
   likewise limited to document IDs selected into the package and are logged
   with the returned excerpt hash. A missing entity, edge, or document is
   reported as outside the captured scope; generation does not silently widen
   the snapshot. Each writer edits only its assigned page under `docs/`.
8. **Write the overview from module pages.** Module pages are written first;
   the Pega overview writer then summarizes and links them. It adds a Mermaid
   diagram only where the child pages support its labeled relationships.
   Recursive submodule generation can extend the module tree for a large
   module.
9. **Add inventories and validate output.** After the model writes the prose,
   CodeWiki appends a deterministic `## Source inventory` section to each
   module page. This is an audit list produced from the package and validated
   plan, not extra model-written explanation:

   - **Owned rules** lists every rule assigned to that page by its name, PEGA
     rule type, exact graph ID, and links to the official Markdown document(s)
     that describe it. This lets a reviewer compare page coverage with
     `plan.json` and open the source for any listed rule.
   - **Directed relationships** lists each captured edge whose source is one
     of those owned rules: source ID → relationship type → target ID, plus a
     link to that edge's receipt. The receipt preserves the original edge
     properties and supporting-document reference. For a focused slice, this
   means edges present in that slice; it does not claim that every project
   edge is listed or that a configured relationship ran at runtime.

   For example, the PoC page
   `docs/Agency_Lookup_Dispatch_and_Validation.md` lists `CercaAgenzia` and
   its official Markdown under *Owned rules*. Under *Directed relationships*
   it lists edges such as `CercaAgenzia — CALLS_ACTIVITY → ...` with their
   step paths and edge-receipt links. The prose above that appendix explains
   the workflow; the inventory below it lets a reviewer check which rules and
   captured edges the page was assigned to cover.

   The generator then checks that required module pages exist, every owned
   rule has official-document evidence, required citations and local links
   resolve, and module ownership is complete. The run-level
   `evidence-manifest.json` answers a different question: it records which
   snapshot and project the run used, hashes and storage locations, which
   evidence sections the writer read, MCP requests, and model-call telemetry.
   On an incremental refresh it also records what changed, which pages were
   reused or regenerated, and why a full-generation fallback was needed.
10. **Build the local viewer.** CodeWiki writes index.html and copies a pinned
    Mermaid browser bundle into assets/. The viewer bundles generated wiki
    pages and fetches official Markdown on demand from evidence/; it verifies
    the source hash. The PEGA CLI disables remote Mermaid validation; diagrams
    are rendered locally by the viewer from Markdown.

### Generate the entire PEGA project

From a local clone of the PEGA Agent repository with its project KB restored
and indexed in Neo4j, start the single generation command through the PEGA
Agent's Python environment. No seed, depth, or manually selected graph branch
is needed for whole-project documentation:

```bash
codewiki pega-generate \
  --project "$PEGA_PROJECT_ID" \
  --mcp-command "$PEGA_PYTHON" \
  --mcp-arg=-m --mcp-arg=pega_kb.neo4j_mcp \
  --mcp-cwd "$PEGA_KB_ROOT" \
  --model "$CUSTOMER_MODEL_ID" \
  --model-base-url "$CUSTOMER_MODEL_BASE_URL" \
  --api-key-env CUSTOMER_MODEL_API_KEY \
  --output runs/pega-project-wiki
```

For each leaf entry in `docs/module_tree.json`, its `components` array contains
the exact IDs of the documented PEGA rules assigned to that page. CodeWiki
uses those IDs to select compatibility components from the package. The PEGA
writer receives compact entity cards and all captured directed relationships
touching those rules, including links to rules owned by other modules or
planning batches. The planner also receives compact cards for connected
endpoints to use as grouping context; those cards never become page owners.
The writer opens relevant official Markdown sections by `document_id`
through `read_pega_evidence`. The full Markdown is not placed in every prompt.
The optional specialist searches and traverses only the captured package in
memory. It cannot add entities or edges by querying Neo4j after capture, and
Markdown readers can open only package-selected document IDs. Parent pages
and the overview are assembled from child pages.
For a large project, planning runs in bounded groups and the overview includes
a complete module index.

### Start generation from an MCP chat

The fork registers a PEGA-only MCP tool named `generate_pega_docs`. Start the
MCP server with `scripts/pega_codewiki_mcp.sh`; that wrapper loads the fork's
`.env.local` and sets the fork checkout first on `PYTHONPATH`. This matters
because `codewiki mcp` by itself starts whichever CodeWiki installation its
`codewiki` executable points to. The ordinary CodeWiki MCP tools analyze
source-code repositories; this added tool is the one that calls the PEGA Agent
server.

Configure an MCP client such as Cursor or Claude Desktop with:

```json
{
  "mcpServers": {
    "codewiki-pega": {
      "command": "/absolute/path/to/codewiki-pega/scripts/pega_codewiki_mcp.sh",
      "args": []
    }
  }
}
```

Then ask for the complete project by name. With no seed, the tool documents the
configured project. For a focused request, name the PEGA activity/rule and the
desired depth, for example “Document `CercaAgenzia` and its connected workflow
to depth 2.” The tool checks the project revision and reuses matching evidence
from its persistent cache. A complete cached project package can provide a
focused slice locally. Otherwise the tool resolves the exact name with
`search_entities`; if ambiguous, it returns candidates for the chat to clarify.
It uses `get_entity` for bounded traversal and `get_document` for the selected
official Markdown, then writes from that package. Matching pages from the
previous run for the same scope are reused automatically. Pass `replan=true`
to reuse evidence but rerun planning and every writer. The response includes
the evidence-cache decision, output directory, and viewer path. A seed's
relationship types can be limited explicitly; when omitted, traversal follows
all domain relationship types up to the chosen depth and enforces the document
limit.

This wrapper is a local stdio server for MCP-capable IDE/chat clients. ChatGPT
web cannot connect directly to local stdio; it requires a remote MCP endpoint
and, for a private WSL server, an approved secure tunnel or remote deployment.
Current ChatGPT plan/admin requirements are described in
[OpenAI's MCP connection guide](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt).

### Run artifacts

| Artifact | Purpose |
| --- | --- |
| evidence-package.json | Captured project or focused slice, scope, selected-document manifest, and snapshot key |
| evidence/ | Links to original PEGA Agent Markdown and stores small metadata records; edges/ holds generated relationship receipts |
| plan.json | Validated module ownership, rationale, and supporting entities |
| docs/module_tree.json | Final documentation hierarchy |
| docs/first_module_tree.json | Initial hierarchy before recursive submodule additions |
| docs/*.md | Final overview and module pages with citations and source inventories |
| docs/metadata.json | Generator, model, project, component counts, and generated page list |
| evidence-manifest.json | Retrieval mode, evidence storage mode, graph/document identity, prompt/implementation, evidence reads, requests, and model telemetry |
| index.html and assets/ | Navigable local wiki and Mermaid bundle |

The named v3 example predates source-linked evidence storage, so its existing
`evidence/` Markdown files remain bundled in that historical run. New captures
and runs link directly to PEGA Agent source Markdown by default.
`--bundle-evidence` is the explicit opt-in for portable copies.

## Refresh documentation incrementally

Run the same `pega-generate` command—or ask the MCP tool for the same
project/scope—into a new empty output directory. CodeWiki checks the current
project manifest hash (or existing graph projection tokens) first. If the exact evidence package and generation
contract are unchanged, it reuses the previous plan and pages without graph
traversal, document retrieval, component adaptation, or model calls. If the
graph revision changed, it captures the requested scope, compares entities,
typed relationships, Markdown hashes, and document metadata, then reuses
unaffected pages and regenerates affected modules. If the prompt, code, model,
selection scope, or ownership changes, it replans and regenerates as needed.
The decision and its reasons are recorded in `evidence-manifest.json`.

Use `--replan` in the CLI or `replan=true` in the MCP tool to force planning and
all page writers to run again while reusing the current evidence package. The
cache is project- and scope-specific; a focused request after a cached complete
project can be selected locally, while a request outside cached evidence causes
the appropriate graph and document retrieval.

Use `--bundle-evidence` only if the generated wiki must move independently of
the shared cache and PEGA Agent source tree. It stores selected Markdown and
metadata copies in the wiki run. By default, the cache stores metadata and
hashes while Markdown links point to the original PEGA Agent files.

Review docs/ as the final wiki. raw-docs/, when included in a run, preserves
writer-stage pages before the run's final link and source-inventory
post-processing.

## Differences from CodeWiki's usual codebase route

| Concern | Codebase source mode | PEGA source mode |
| --- | --- | --- |
| Source facts | Local code and optional build/configuration files | Published Neo4j graph and official PEGA Agent Markdown selected through graph IDs |
| Dependency graph construction | DependencyGraphBuilder and DependencyParser analyze source files; language analyzers use TreeSitter/ASTs | PEGA Agent has already projected typed relationships into Neo4j; CodeWiki captures them through MCP. Repository parsing and TreeSitter are bypassed |
| Analysis artifact | CodeWiki dependency-graph JSON and leaf components for clustering | No standard dependency-graph JSON; evidence-package.json preserves typed edges, with a temporary CodeWiki Node compatibility view |
| Module planning | LLM clustering and super-grouping use the source dependency graph; agents can add submodules | Pega planner groups captured documented rules by capability and validates exact ownership for this snapshot; agents can add submodules |
| Writer evidence tools | read_code_components reads source components | read_pega_evidence reads package-selected Markdown; specialist search and traversal stay within the captured graph |
| Meaning of a link | A static dependency/reference extracted from source code | A typed Pega configuration relationship with direction, qualifiers, and provenance; it is not an execution trace |
| Refresh | CodeWiki compares repository changes and refreshes affected docs | One `pega-generate`/MCP request checks the project manifest or row-token revision, reuses or refreshes scoped evidence, and incrementally reuses pages for the same scope |
| Optional artifact analysis | Build, CI, container, and configuration artifacts can add nodes | Disabled by the PEGA generator; upstream PEGA graph/document evidence is used |
| Mermaid | Normal CodeWiki writing can validate Mermaid before saving | PEGA route disables remote validation; local viewer renders with its bundled JavaScript |

Both routes share the module writer framework, module tree, overview, metadata,
and local wiki viewer. PEGA mode changes the source adapter, graph/evidence
contract, module planning rules, writer instructions, and available tools.

## Evidence review rules

- A graph edge is configured behavior, not proof of runtime execution, API
  success, or a committed write.
- Keep deterministic extracted configuration distinct from upstream
  functional synthesis, which is an interpretation.
- Preserve MISSING_EXPORT and AMBIGUOUS_REFERENCE boundaries; do not infer a
  missing rule's implementation from its name.
- Verify substantive wiki claims against the cited Markdown or relationship
  receipt.
- The PoC is complete for its requested scope only. Do not present its counts
  as totals for the whole unipolLead KB.
- Treat each run's evidence-manifest.json as authoritative for the model,
  prompt, and source implementation used to create that run.

## Implementation references

- [PEGA integration workflow](pega-integration.md)
- [PegaDocumentationGenerator](../codewiki/src/be/pega_documentation_generator.py)
- [Pega graph provider and evidence cache](../codewiki/src/be/sources/pega_mcp.py)
- [Pega module planner](../codewiki/src/be/pega_planner.py)
- [Pega writer and overview prompts](../codewiki/src/be/pega_prompts.py)
- [Pega local viewer](../codewiki/cli/pega_viewer.py)
- [PEGA Agent KB skill](../../pega-kb-codex-unipolLead/.codex/skills/pega-kb/SKILL.md)
- [PEGA graph navigation guidance](../../pega-kb-codex-unipolLead/.codex/skills/pega-kb/references/pega-navigation.md)
