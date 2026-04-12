# Managed Agent Orchestrator

Covers `managed_agent.py` — the Claude Agent SDK orchestrator that runs the full revenue analysis pipeline as a managed agent.

## Overview

`managed_agent.py` wraps the 6-step pipeline into tool functions that Claude can call via the Claude Agent SDK (`claude_agent_sdk`). Claude orchestrates the pipeline in order, handles errors, retries failed steps once, and reports completion status.

## Architecture

```
managed_agent.py
  ├── 6 tool functions (MCP server)
  │     ├── ingest_data          → wraps ingest.py
  │     ├── ingest_transcripts   → wraps transcript_ingest.py
  │     ├── analyze_transcripts  → wraps transcript_analyzer.py
  │     ├── run_analysis         → wraps agent.py
  │     ├── generate_dashboard   → wraps dashboard.py
  │     └── export_to_excel      → wraps export.py
  │
  ├── System prompt (built from skills/*.md files)
  │
  └── query() → Claude orchestrates tool calls
```

## SDK Components Used

- **`@tool` decorator** — defines each pipeline step as a callable tool with name, description, and input schema
- **`create_sdk_mcp_server`** — bundles all 6 tools into a single MCP server named "pipeline"
- **`query()`** — starts the agentic loop; Claude reads the system prompt, sees the available tools, and calls them in sequence
- **`ClaudeAgentOptions`** — configures model, system prompt, MCP servers, allowed tools, and permissions

## Tool Definitions

### 1. `ingest_data(tickers)`
- Wraps: `ingest.py` logic (FMP API → `revenue_actuals` + `consensus_estimates`)
- Input: `{"tickers": ["SNOW", "DDOG"]}` (optional — defaults to all 5)
- Calls: `fix_q4()`, `rebuild_consensus()` per ticker
- Output: success/failure + stdout capture

### 2. `ingest_transcripts(tickers)`
- Wraps: `transcript_ingest.py` logic (Seeking Alpha → `transcripts` table)
- Input: `{"tickers": ["SNOW"]}` (optional)
- Fetches last 4 transcripts per ticker, maps to DB periods
- Respects 1-second rate limit between API calls

### 3. `analyze_transcripts(tickers)`
- Wraps: `transcript_analyzer.py` logic (anomaly detection + Claude analysis)
- Input: `{"tickers": ["SNOW"]}` (optional)
- For each anomalous quarter with a transcript, sends to Claude for management commentary analysis
- Stores analysis in `transcripts.transcript_analysis` column

### 4. `run_analysis(tickers)`
- Wraps: `agent.py` `run_agent()` per ticker
- Input: `{"tickers": ["SNOW"]}` (optional)
- Computes full analytics: $ QoQ, seasonality, anomalies, projections, beat cadence, guide inference
- Generates Claude research notes, saves to `agent_reports` table
- This is the longest-running step (~60-90 seconds for 5 tickers)

### 5. `generate_dashboard()`
- Wraps: `dashboard.py` `build_data()` + `generate_html()`
- Input: none
- Writes `dashboard.html` to project directory

### 6. `export_to_excel()`
- Wraps: `export.py` `build_all()` + sheet builders
- Input: none
- Writes `kinetic_revenue_model.xlsx` to project directory

## System Prompt

Built dynamically at runtime from two sources:

1. **Runtime config** — which tickers and steps to run
2. **Skills files** — reads every `*.md` file from `skills/` directory and injects their full content as reference documentation

The system prompt instructs Claude to:
- Execute steps in exact order (1→2→3→4→5→6)
- Check success/failure after each tool call
- Retry failed steps exactly once
- Stop the pipeline if a retry also fails
- Report a final summary table with PASS/FAIL/SKIPPED per step

## Error Handling

### Tool-level
Each tool wraps its pipeline script in `_run_script()` which:
- Captures stdout and stderr via `redirect_stdout`/`redirect_stderr`
- Catches all exceptions and returns them in a structured result dict
- Returns `{"success": True/False, "stdout": ..., "stderr": ..., "error": ...}`
- Formats the result into a readable string for Claude

### Agent-level
Claude reads the tool result and decides:
- **Success** → proceed to next step
- **Failure** → retry the same tool once
- **Second failure** → report failure and stop pipeline

This two-tier approach means transient issues (API rate limits, network timeouts) get a retry, but persistent errors (missing dependencies, bad credentials) stop quickly.

## CLI Usage

```bash
# Full pipeline, all 5 tickers
python managed_agent.py

# Specific tickers
python managed_agent.py --tickers SNOW,DDOG

# Specific steps (e.g., just re-run analysis + dashboard)
python managed_agent.py --steps 4,5,6

# Single ticker, single step
python managed_agent.py --tickers SNOW --steps 4
```

### Arguments
| Flag | Default | Description |
|------|---------|-------------|
| `--tickers` | SNOW,DDOG,MDB,TENB,QLYS | Comma-separated ticker list |
| `--steps` | 1,2,3,4,5,6 | Comma-separated step numbers |

## Execution Model

The agent runs on **claude-sonnet-4-6** with:
- `permission_mode="bypassPermissions"` — tools are pre-approved, no interactive prompts
- `max_turns=30` — enough for 6 steps + retries + reporting
- All 6 tools pre-listed in `allowed_tools` with `mcp__pipeline__` prefix
- `cwd` set to the project directory

## Tool Function Pattern

Each tool follows the same pattern:

```python
@tool("name", "description", {"tickers": list[str]})
async def tool_fn(args: dict[str, Any]) -> dict[str, Any]:
    tickers = args.get("tickers") or DEFAULT_TICKERS
    
    def run():
        # Import from original script and call its functions
        from original_script import entry_point
        entry_point(tickers)
    
    result = _run_script("original_script.py", setup_fn=run)
    return {"content": [{"type": "text", "text": _format_result("name", result)}]}
```

Key design choices:
- **Import-and-call** rather than subprocess — avoids shell overhead and captures output cleanly
- **Fresh imports** via `setup_fn` — prevents state leakage between tool calls
- **Structured results** — success/failure flag + captured output enables Claude to make retry decisions
- **Default tickers** — every tool accepts an optional tickers list, defaulting to all 5

## Dependencies

- `claude-agent-sdk>=0.1.58` — Claude Agent SDK for tool definition and agentic query loop
- `anthropic` — used internally by the pipeline scripts for Claude API calls
- `psycopg2`, `requests`, `openpyxl`, `python-dotenv` — used by pipeline scripts

## Output

When the pipeline completes, Claude reports a summary like:

```
Pipeline Execution Summary
| Step | Tool                 | Status  |
|------|----------------------|---------|
| 1    | ingest_data          | PASS    |
| 2    | ingest_transcripts   | PASS    |
| 3    | analyze_transcripts  | PASS    |
| 4    | run_analysis         | PASS    |
| 5    | generate_dashboard   | PASS    |
| 6    | export_to_excel      | PASS    |

All 6 steps completed successfully for SNOW, DDOG, MDB, TENB, QLYS.
```
