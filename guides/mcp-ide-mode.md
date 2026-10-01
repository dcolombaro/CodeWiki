# MCP / IDE-driven mode

CodeWiki can run as a **Model Context Protocol (MCP) server**. Its general
source-code tools need no model configuration: your AI IDE (Cursor, Claude
Desktop, CodeBuddy, Claude Code, or another MCP client) supplies the
reasoning, while CodeWiki supplies dependency analysis, code reading,
module-tree bookkeeping, prompt templates, and a document writer. The PEGA
generation tool added by this fork calls the configured model endpoint.

Use this mode when you already pay for an IDE agent and want documentation
without a second API key, or when you want to steer clustering and writing
style interactively. Use the [CLI](cli-reference.md) when you want a
one-shot, reproducible build.

## Setup

```bash
pip install git+https://github.com/FSoft-AI4Code/CodeWiki.git
codewiki --version
```

Add the server to your IDE's MCP configuration. The same JSON works
everywhere:

```json
{
  "mcpServers": {
    "codewiki": {
      "command": "codewiki",
      "args": ["mcp"]
    }
  }
}
```

Where to put it:

| Client | Location |
| --- | --- |
| Cursor | Settings, then MCP, then Add Server |
| Claude Desktop (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Code | `claude mcp add codewiki -- codewiki mcp` |
| CodeBuddy | The MCP configuration in settings |
| Other stdio MCP clients | The same command and args |

Then, in agent mode, ask:

```
Analyze this repository and generate wiki documentation into docs/.
```

A ready-made skill for Claude-based agents is in
[`skills/codewiki-wiki-generator/SKILL.md`](../skills/codewiki-wiki-generator/SKILL.md).
It tells the agent the workflow below.

## PEGA documentation with this fork

The generic setup above describes upstream CodeWiki. This fork adds
`generate_pega_docs` to the MCP tool list. The original tools
`analyze_repo` and `generate_docs` document source-code repositories; they do
not query the PEGA knowledge graph. The PEGA tool uses the PEGA Agent's
`pega_kb.mcp_server` MCP service and the configured GPT-compatible model endpoint.

Set the model, project, PEGA Agent repository, and PEGA Python environment in
the fork's Git-ignored `.env.local`. See
[`.env.local.example`](../.env.local.example). Start the server through the
fork wrapper so it loads those values and places the fork before any upstream
CodeWiki installation on `PYTHONPATH`:

```json
{
  "mcpServers": {
    "codewiki-pega": {
      "command": "/home/USER/projects/Genertel/codewiki-pega/scripts/pega_codewiki_mcp.sh",
      "args": []
    }
  }
}
```

Replace the path with the checkout location. In the chat, ask for the whole
project:

```text
Generate the documentation for the UnipolLead PEGA project.
```

With no seed, `generate_pega_docs` targets the complete configured project.
For a focused request, name one exact PEGA rule, for example:

```text
Document the CercaAgenzia activity and its connected workflow to depth 2.
```

The chat agent passes `seed_name` and `depth=2`. On every invocation, CodeWiki
checks the live project manifest through Neo4j (or existing row projection
tokens for older KBs). If the exact scope and revision are cached, it reuses
the evidence package and precomputed CodeWiki component view. If a complete project package is cached,
a focused slice can be derived locally. Otherwise CodeWiki resolves the seed,
captures the requested graph scope and retrieves its selected Markdown, then
checks the project revision again before saving the package. The package is
then fixed for planning and writing. Matching prior pages are reused
automatically; pass `replan=true` to rerun planning and every page
writer against the cached evidence. The tool returns the output directory,
viewer path, and whether evidence was reused, derived, or recaptured.
`depth`, `relationship_types`, `expand_entity_ids`, and `max_documents` apply
only to a focused seed. With the default scope, the complete project is used.

This wrapper starts a local stdio MCP server for clients such as Cursor or
Claude Desktop. ChatGPT web does not connect directly to local stdio servers;
it requires a remote MCP endpoint. To use this tool from ChatGPT, expose the
server through an approved secure MCP tunnel or remote MCP deployment. See
[OpenAI's ChatGPT MCP connection requirements](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt).
OpenAI currently documents write-capable custom MCP apps as a beta for
Business, Enterprise, and Edu workspaces; Pro access is limited to read/fetch
actions. Workspace admin settings also control whether a custom app can be
created and used.

## The tools

Eight general-purpose fine-grained tools do not call an LLM. The PEGA
generation tool calls the configured model. Two legacy tools are also
available.

| Tool | What it does |
| --- | --- |
| `analyze_repo` | Parse the repository, build the dependency graph, detect changes since the last run. Writes the component index, leaf nodes, and language stats to a workspace folder and returns the paths plus a summary |
| `read_code_components` | Write the source of the requested components to `sources/<id>.src` in the workspace and return the paths |
| `get_prompt` | Return the prompt template for a pipeline stage: `cluster`, `system_leaf`, `overview_module`, `overview_repo` |
| `save_module_tree` | Store the agent's module clustering as `module_tree.json` and `first_module_tree.json`; compute the processing order; warn about orphaned or unassigned component ids |
| `get_processing_order` | Leaf-first order in which modules should be written |
| `write_doc_file` | Create a Markdown page. Mermaid diagrams are validated before the write |
| `edit_doc_file` | `str_replace`, `insert`, or `undo` on an existing page, with per-file edit history |
| `close_session` | Write `metadata.json`, delete the workspace, free memory |
| `generate_pega_docs` (fork only) | Capture the whole PEGA project or an exact seeded slice through the PEGA Agent MCP, then generate an evidence-linked wiki with the configured model |
| `generate_docs` (legacy) | One-shot generation through CodeWiki's own agents. Needs `codewiki config set` |
| `get_module_tree` (legacy) | Read an existing `module_tree.json` |

**File side-channel.** Large payloads never go through the MCP channel. The
server writes them to `<repo>/.codewiki/sessions/<session_id>/` and returns
file paths. The agent reads those files with its own file tools. There is no
truncation, however large the repository.

```
.codewiki/sessions/<session_id>/
├── component_index.json         # id, type, file for every component
├── leaf_nodes.json              # component ids that can become documentation units
├── languages.json               # language statistics
├── summary.json                 # compact analysis summary
├── changes.json                 # change detection result (when docs exist)
├── processing_order.json        # after save_module_tree
├── module_tree_validation.json  # after save_module_tree
└── sources/<sanitized_id>.src   # after read_code_components
```

Sessions expire after 2 hours without activity. At most 10 sessions are kept;
the oldest is evicted. Expired and evicted sessions have their workspace
removed. All paths written by `write_doc_file` and `edit_doc_file` are checked
to stay inside the session's output directory.

## The agent workflow

```
1. analyze_repo
   read component_index.json, leaf_nodes.json, languages.json

2. get_prompt("cluster") + read_code_components + save_module_tree
   the agent groups components into 3-8 modules and saves the tree
   read processing_order.json

3. for each leaf module, in order:
     get_prompt("system_leaf")
     read_code_components -> read sources/*.src
     write_doc_file        -> <module>.md (Mermaid validated)
   for each parent module:
     read the child pages
     get_prompt("overview_module")
     write_doc_file        -> <parent>.md

4. get_prompt("overview_repo") -> write_doc_file overview.md

5. close_session -> metadata.json written, workspace removed
```

Always finish with `close_session`. It writes `metadata.json`, and that file
is the baseline for the next incremental run.

## Incremental updates in MCP mode

When documentation already exists, `analyze_repo` compares the repository
against `metadata.json` and returns a `changes` field:

```json
{
  "changes": {
    "has_previous": true,
    "no_changes": false,
    "method": "git",
    "changed_files": ["auth.py"],
    "affected_modules": ["Authentication Module"],
    "cascade_modules": ["Core System", "overview"],
    "hint": "Only 1 module(s) need updating: ..."
  }
}
```

Detection uses Git when it can (diff against the stored commit, plus
uncommitted and untracked files) and file modification times otherwise. The
agent then re-reads only the components of `affected_modules`, patches those
pages with `edit_doc_file`, refreshes the parents in `cascade_modules`, and
updates `overview.md`.

This detection is **module-level**: a changed source file marks its whole
module. The CLI's `codewiki generate --update` uses a finer,
**component-level** updater that diffs the dependency graph and repairs the
module tree. See [Incremental updates](incremental-updates.md). Both modes
write the same `metadata.json`, so you can mix them.

## Output

The same layout as the CLI produces:

```
docs/
├── overview.md
├── <module>.md ...
├── module_tree.json
├── first_module_tree.json
└── metadata.json
```

## FAQ

**The server does not start, missing dependencies.**
Run `pip install -e .` (or the `pip install git+...` line above). The MCP
server does not need the CLI-only packages, but it does need the tree-sitter
grammars and the `mcp` package.

**`analyze_repo` is slow.**
Tree-sitter parsing of a repository above 100k lines usually finishes within
30 seconds. Pass `include_patterns` or `exclude_patterns` to narrow the
scope.

**Mermaid validation errors.**
The agent gets the validator's message back and fixes the diagram. If every
diagram fails, check that `mermaid-py` is installed.

**Documentation in another language.**
Tell the agent: "Write the wiki in English" or "Use Chinese for the
documentation."

**Session timed out.**
Call `analyze_repo` again. It creates a new session.

**I want the old one-shot behaviour inside the IDE.**
Run `codewiki config set ...` once, then ask the agent to call
`generate_docs`.
