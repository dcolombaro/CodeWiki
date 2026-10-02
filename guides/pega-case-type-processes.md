# Case Type process documentation

Use a Case Type seed when the requested wiki is about one PEGA business
process. A Case Type is the process entry point: its official Markdown lists
ordered stages and their Flow rules. CodeWiki follows the captured graph from
that seed and organizes the documentation around the process instead of
grouping rules only by technical class or ruleset.
This follows [Pega's Case Lifecycle model](https://academy.pega.com/topic/case-lifecycle/v7),
which organizes a Case Type into stages, processes, and steps.

## iLove example in GTLLife

The GTLLife Case Type `iLove` has three primary stages in source order:
`Initalize` (shown to readers as **Initialization**), **Origination**, and
**Finalization**. Its alternate stages are **BackToPreviousScreen** and
**TechnicalError**. The source spelling and `step_path` values remain in the
evidence package. `STARTS_FLOW` edges connect the Case Type to each stage's
Flow rule.

From the CodeWiki fork directory, with `.env.local` configured for the PEGA
bundle and approved model endpoint:

```bash
set -a
. ./.env.local
set +a
"$PEGA_PYTHON" -m pega_kb.instance start
codewiki pega-generate \
  --project GTLLife --release gtllife \
  --mcp-command "$PEGA_PYTHON" \
  --mcp-arg=-m --mcp-arg=pega_kb.mcp_server \
  --mcp-cwd "$PEGA_KB_ROOT" \
  --seed-id 'GTLLife::GTLLife::26.06.01|rule:d2b86fc09685' \
  --depth 12 --traversal-direction outgoing \
  --relationship-type CALLS \
  --relationship-type READS \
  --relationship-type WRITES \
  --max-documents 500 \
  --model "$CUSTOMER_MODEL_ID" \
  --model-base-url "$CUSTOMER_MODEL_BASE_URL" \
  --api-key-env CUSTOMER_MODEL_API_KEY \
  --doc-type functional \
  --output runs/GTLLife_iLove_next_run
```

The completed `02102026_GTLLife_iLove_old_graph` demonstration used optional
editorial `--instructions` to emphasize business steps and service boundaries;
the Case Type parent and stage order come from the planner and source evidence,
not those instructions. Use a new empty `--output` directory for another run. A refresh can
reference a completed run through `--incremental-from` when its scope and
documentation profile are compatible.

That seed and graph direction select the documented dependencies used by this
Case Type. They do not mean the whole GTLLife release was captured. In the
2026-10-02 input revision, this scope contained 250 entities, 471 directed
relationships, and 210 Markdown references; a later projection may differ.
Depth 12 reached the dependency closure in this revision. The document limit
is a guard on focused captures, not a target count.

The completed run has one `iLove_process` parent, ordered stage sections,
shared support, 40 leaf chapters, and four parent summaries. Its 210
documented rules all have one primary leaf owner. The run also has the
CodeWiki root overview, for 45 Markdown pages in total. Open its generated
viewer with:

```bash
codewiki pega-serve \
  --run-dir /home/dcolombaro/projects/Genertel/codewiki-pega/runs/02102026_GTLLife_iLove_old_graph \
  --port 8768
```

Then visit <http://127.0.0.1:8768/index.html>. Stop the server with Ctrl-C.

## How the hierarchy is built

1. The focused capture records graph IDs, typed directed edges, edge
   properties, Markdown source paths and hashes in `evidence-package.json`.
2. The Case Type planner reads the selected Case Type Markdown and matches
   stage process entries to captured `STARTS_FLOW` edges by `call_site_id`.
3. It follows captured outgoing `CALLS`, `READS`, and `WRITES` links from each
   stage Flow. A rule reached most closely from one stage belongs to that
   stage. Equally close reusable rules get one shared owner; case-wide or
   unreachable rules belong to the process lifecycle chapter.
4. Large stages are subdivided into a Flow journey chapter and supporting
   chapters. Discovery requests may be split between whole rules to keep
   model requests manageable. No rule's semantic Markdown section is cut;
   the reconciliation pass sees all candidates and assigns every documented
   rule once.
5. The resulting `docs/module_tree.json` controls leaf writing and viewer
   navigation. Parent pages summarize children. Each writer can read its
   owned rules' Markdown and the relevant edge evidence, including named
   services and external references where the input provides them.

Inspect `plan.json` for `planning.mode = case_type_stage_hierarchy`,
`planning.stage_order`, and `primary_owner`. Compare the resulting pages with
the Case Type Markdown and Flow evidence before treating any functional
description as verified runtime behavior.

The complete `unipolLead` / `lead` generation remains a separate use case:
its release currently lacks Case Type and Flow anchors and continues to use
the global capability planner described in the
[complete lead runbook](pega-runbook.md).
For a complete functional release that does contain documented Case Types,
the same stage anchoring runs automatically for each Case Type without a seed
or custom `--instructions`. Rules reached by multiple cases share a separate
capability area; documented rules outside the case journeys remain in another
capability area. A focused run still uses `--seed-id` to bound
the evidence package to one requested process.
