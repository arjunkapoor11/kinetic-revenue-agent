"""
Kinetic Revenue Agent — Managed Agent orchestrator.

Uses the Anthropic API tool-use loop to let Claude orchestrate the 6-step
pipeline. Claude calls tools in order, handles errors, retries failed steps
once, and reports completion status.

Usage:
    python managed_agent.py                         # run full pipeline
    python managed_agent.py --tickers SNOW,DDOG     # subset of tickers
    python managed_agent.py --steps 1,2,3           # specific steps only
"""

import argparse
import io
import os
import sys
import time
import traceback
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

PROJECT_DIR = Path(__file__).parent
DEFAULT_TICKERS = [
    "SNOW", "DDOG", "MDB", "TENB", "QLYS",
    "NOW", "ADBE", "INTU", "WDAY", "PANW",
    "CDNS", "SNPS", "ADSK", "APP", "FTNT",
    "TEAM", "VEEV", "ROP", "PLTR",
    "HUBS", "ZS", "CRWD", "OKTA", "TTD",
    "GTLB", "BILL", "MNDY", "CFLT", "ESTC",
    "FROG", "S", "ZI", "DT", "TOST",
    "PCTY", "PAYC", "GWRE", "BSY", "CWAN",
    "NCNO", "BRZE", "KVYO", "PCOR", "SPSC",
    "MANH", "AZPN", "APPN", "DOCN",
]

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── tool execution engine ──────────────────────────────────────────────────

def _capture(fn):
    """Run fn(), capturing stdout/stderr and exceptions."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            fn()
        return {"success": True, "stdout": out.getvalue(), "stderr": err.getvalue()}
    except Exception:
        return {
            "success": False,
            "stdout": out.getvalue(),
            "stderr": err.getvalue(),
            "error": traceback.format_exc(),
        }


def _fmt(name, r):
    status = "SUCCESS" if r["success"] else "FAILED"
    parts = [f"[{name}] {status}"]
    if r["stdout"].strip():
        # Truncate very long output to stay within context limits
        text = r["stdout"].strip()
        if len(text) > 3000:
            text = text[:1500] + "\n... (truncated) ...\n" + text[-1500:]
        parts.append(f"Output:\n{text}")
    if r["stderr"].strip():
        parts.append(f"Stderr:\n{r['stderr'].strip()[:1000]}")
    if not r["success"] and r.get("error"):
        parts.append(f"Error:\n{r['error'][:2000]}")
    return "\n".join(parts)


# ── tool implementations ──────────────────────────────────────────────────

def exec_ingest_data(tickers):
    def run():
        sys.path.insert(0, str(PROJECT_DIR))
        import ingest
        # Override the module's TICKERS list so fetch_and_store() processes
        # only the requested tickers.
        ingest.TICKERS = list(tickers)
        ingest.fetch_and_store()

    return _fmt("ingest_data", _capture(run))


def exec_ingest_transcripts(tickers):
    def run():
        sys.path.insert(0, str(PROJECT_DIR))
        from transcript_ingest import (
            get_db_connection, get_transcript_list,
            get_transcript_content, resolve_period,
        )

        conn = get_db_connection()
        cur = conn.cursor()
        for ticker in tickers:
            print(f"\nProcessing {ticker}...")
            try:
                articles = get_transcript_list(ticker)
            except Exception as e:
                print(f"  Error fetching list for {ticker}: {e}")
                continue
            print(f"  Found {len(articles)} transcripts")
            for article in articles:
                period = resolve_period(ticker, article, cur)
                if not period:
                    print(f"  Skipping — could not determine period")
                    continue
                try:
                    text = get_transcript_content(article["id"])
                except Exception as e:
                    print(f"  Error fetching transcript: {e}")
                    continue
                if len(text) < 500:
                    continue
                cur.execute(
                    "INSERT INTO transcripts (ticker,period,transcript) "
                    "VALUES (%s,%s,%s) ON CONFLICT (ticker,period) "
                    "DO UPDATE SET transcript=EXCLUDED.transcript",
                    (ticker, period, text),
                )
                print(f"  Stored: {article['title']} -> {period} ({len(text):,} chars)")
                time.sleep(1)
        conn.commit()
        cur.close()
        conn.close()
        print("\nAll transcripts ingested")

    return _fmt("ingest_transcripts", _capture(run))


def exec_analyze_transcripts(tickers):
    def run():
        sys.path.insert(0, str(PROJECT_DIR))
        from transcript_analyzer import (
            ensure_analysis_column, get_db_connection, compute_qoq,
            compute_seasonality, flag_anomalies, get_transcript,
            analyze_transcript,
        )

        ensure_analysis_column()
        conn = get_db_connection()
        cur = conn.cursor()
        for ticker in tickers:
            print(f"\n  {ticker} — Transcript Anomaly Analysis")
            cur.execute(
                "SELECT period,revenue FROM revenue_actuals WHERE ticker=%s ORDER BY period",
                (ticker,),
            )
            actuals = [{"period": str(r[0]), "revenue": r[1]} for r in cur.fetchall()]
            if len(actuals) < 2:
                print(f"  Insufficient data"); continue
            qoq = compute_qoq(actuals)
            seasonal = compute_seasonality(qoq)
            anomalies = flag_anomalies(qoq, seasonal)
            if not anomalies:
                print(f"  No anomalies detected"); continue
            print(f"  Found {len(anomalies)} anomalous quarters")
            for anomaly in anomalies:
                period = anomaly["period"]
                transcript = get_transcript(ticker, period, cur)
                if not transcript:
                    print(f"  No transcript for {period} — skipping"); continue
                try:
                    analysis = analyze_transcript(ticker, anomaly, transcript)
                except Exception as e:
                    print(f"  Error analyzing {period}: {e}"); continue
                cur.execute(
                    "UPDATE transcripts SET transcript_analysis=%s WHERE ticker=%s AND period=%s",
                    (analysis, ticker, period),
                )
                print(f"  Analyzed {period} ({len(analysis):,} chars)")
        conn.commit()
        cur.close()
        conn.close()
        print("\nAll transcript analyses complete")

    return _fmt("analyze_transcripts", _capture(run))


def exec_run_analysis(ticker):
    def run():
        sys.path.insert(0, str(PROJECT_DIR))
        from agent import run_agent
        run_agent(ticker)

    return _fmt(f"run_analysis({ticker})", _capture(run))


def exec_generate_dashboard():
    def run():
        sys.path.insert(0, str(PROJECT_DIR))
        from dashboard import build_data, generate_html
        data = build_data()
        html = generate_html(data)
        path = str(PROJECT_DIR / "dashboard.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Dashboard written to {path}")

    return _fmt("generate_dashboard", _capture(run))


def exec_export_to_excel():
    def run():
        sys.path.insert(0, str(PROJECT_DIR))
        from export import build_all, build_summary_sheet, build_ticker_sheet, TICKERS
        from openpyxl import Workbook
        all_data = build_all()
        wb = Workbook()
        build_summary_sheet(wb, all_data)
        for tk in TICKERS:
            if tk in all_data:
                build_ticker_sheet(wb, tk, all_data[tk])
        path = str(PROJECT_DIR / "kinetic_revenue_model.xlsx")
        wb.save(path)
        print(f"Exported to {path}")

    return _fmt("export_to_excel", _capture(run))


# ── tool dispatcher ────────────────────────────────────────────────────────

TOOL_DISPATCH = {
    "ingest_data": lambda args: exec_ingest_data(args.get("tickers", DEFAULT_TICKERS)),
    "ingest_transcripts": lambda args: exec_ingest_transcripts(args.get("tickers", DEFAULT_TICKERS)),
    "analyze_transcripts": lambda args: exec_analyze_transcripts(args.get("tickers", DEFAULT_TICKERS)),
    "run_analysis": lambda args: exec_run_analysis(args.get("ticker", "")),
    "generate_dashboard": lambda args: exec_generate_dashboard(),
    "export_to_excel": lambda args: exec_export_to_excel(),
}


# ── tool schemas ───────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "ingest_data",
        "description": (
            "Step 1: Ingest quarterly revenue actuals and consensus estimates "
            "from FinancialModelingPrep API into PostgreSQL. Fixes Q4 cumulative "
            "data and rebuilds consensus estimates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ticker symbols to process. Defaults to all 5 if omitted.",
                }
            },
        },
    },
    {
        "name": "ingest_transcripts",
        "description": (
            "Step 2: Fetch earnings call transcripts from Seeking Alpha (via "
            "RapidAPI) and store in PostgreSQL. Gets the last 4 transcripts per ticker."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ticker symbols to process.",
                }
            },
        },
    },
    {
        "name": "analyze_transcripts",
        "description": (
            "Step 3: Analyze earnings call transcripts for anomalous quarters "
            "using Claude. For each anomalous quarter with a transcript, identifies "
            "management commentary explaining revenue distortions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ticker symbols to process.",
                }
            },
        },
    },
    {
        "name": "run_analysis",
        "description": (
            "Step 4: Run full quantitative analysis and generate a Claude research "
            "note for a SINGLE ticker. Call once per ticker (~60-90 seconds each). "
            "Computes $ QoQ trends, seasonality, anomaly detection, projections, "
            "beat cadence, guide inference. Saves report to DB."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "A single ticker symbol (e.g. \"SNOW\"). Required.",
                }
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "generate_dashboard",
        "description": (
            "Step 5: Generate the interactive HTML dashboard (dashboard.html). "
            "Reads data from PostgreSQL and writes a self-contained HTML file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "export_to_excel",
        "description": (
            "Step 6: Export the revenue model to kinetic_revenue_model.xlsx. "
            "Professional financial model with time-series layout and live formulas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ── system prompt ──────────────────────────────────────────────────────────

def build_system_prompt(tickers, steps):
    skills_dir = PROJECT_DIR / "skills"
    # Load only the key skills files (not the full 40K+ of all docs)
    key_files = ["SKILL.md", "data-ingestion.md", "analysis-framework.md"]
    skills_parts = []
    for name in key_files:
        path = skills_dir / name
        if path.exists():
            content = path.read_text(encoding="utf-8")
            # Truncate very long files to keep prompt reasonable
            if len(content) > 4000:
                content = content[:4000] + "\n... (truncated)"
            skills_parts.append(f"### {name}\n{content}")
    skills_text = "\n\n".join(skills_parts)

    tickers_str = ", ".join(tickers)
    steps_str = ", ".join(str(s) for s in steps) if steps else "all (1-6)"

    return f"""You are the Kinetic Revenue Agent orchestrator. Execute the revenue analysis
pipeline for the specified tickers and report completion status after each step.

## Configuration
- Tickers: {tickers_str}
- Steps: {steps_str}

## Pipeline (execute in this exact order)
1. ingest_data — Pull revenue actuals + consensus from FMP API (pass tickers array)
2. ingest_transcripts — Fetch earnings call transcripts from Seeking Alpha (pass tickers array)
3. analyze_transcripts — Call ONCE PER TICKER with ticker="SYMBOL". Do NOT pass an array.
4. run_analysis — Call ONCE PER TICKER with ticker="SYMBOL". Do NOT pass an array.
5. generate_dashboard — Write dashboard.html (no arguments)
6. export_to_excel — Write kinetic_revenue_model.xlsx (no arguments)
7. post_to_slack — Post guide signal summary to Slack (no arguments)

## Standalone Tools (not part of the pipeline — call on demand)
- earnings_prep(ticker="SYMBOL") — Returns structured JSON with revenue estimates, consensus, beat cadence, anomalies, implied options move, and transcript analyses for a ticker. When this tool is called, use the returned data to write a comprehensive earnings prep document with these sections:
  1. **THE SETUP AT A GLANCE**: A summary table with one row per metric. Include these rows:
     | Metric               | Value |
     |----------------------|-------|
     | Q+1 Consensus        | $XXX.XM |
     | Our Estimate (Beat-adj) | $XXX.XM |
     | Expected Beat        | +X.X% ($X.XM) |
     | STL Projection       | $XXX.XM |
     | YoY Growth           | +XX.X% |
     | Beat Cadence         | X.X% avg (NQ window) |
     | Momentum             | ACCELERATING / STABLE / DECELERATING |
     | Options Implied Move | ±X.X% (expires YYYY-MM-DD) |
     Use the implied_move data from the JSON: implied_move_pct * 100 for the percentage, implied_move_expiry for the date. If implied_move is null, show "N/A — options data unavailable".
  2. **Revenue Setup**: Deeper narrative on our estimates (STL + beat-adjusted) vs consensus, expected beat $/%,  YoY growth, beat cadence, momentum. Include: "Options market is pricing a ±X.X% move into earnings (expires [date])" — compare to historical beat cadence to flag if market expectations seem high or low relative to typical beats.
  3. **Q+2 Guide Inference**: Implied guide, consensus, gap %, GUIDE ABOVE/BELOW/IN-LINE signal
  4. **Historical Anomalies**: Anomalous quarters with sigma deviation, $ QoQ, transcript analysis context — classify as one-time vs structural
  5. **Key Questions for the Call**: 5 targeted questions based on transcript analysis — focus on guide conservatism, deal clustering follow-through, NRR/expansion, product drivers, macro sensitivity
  6. **Metrics to Watch**: 3-5 key metrics management typically calls out, flagging unusual patterns
  7. **Prior Quarter Recap**: What happened last quarter — beat/miss, transcript highlights, surprises

## Rules
- Execute only the steps specified.
- Steps 1-2: pass the full tickers array.
- Steps 3-4: call once per ticker. Each call takes ~60-90 seconds.
- Steps 5-7 take no arguments.
- After each tool call, check if it succeeded or failed.
- If a step FAILS: retry it exactly ONCE. If retry also fails, STOP — do not continue.
- If a step succeeds: report briefly and continue to the next step.
- After all steps, output a final summary table: Step | Tool | Status (PASS/FAIL/SKIPPED).

## Project Reference
{skills_text}
"""


# ── agentic loop ───────────────────────────────────────────────────────────

def run_agent(tickers, steps):
    system = build_system_prompt(tickers, steps)
    tickers_str = ", ".join(tickers)
    steps_str = ", ".join(str(s) for s in steps) if steps else "1 through 6"

    messages = [
        {
            "role": "user",
            "content": (
                f"Execute the revenue analysis pipeline for tickers: {tickers_str}. "
                f"Run steps {steps_str}. Pass tickers=[{', '.join(repr(t) for t in tickers)}] "
                f"to each tool that accepts it. Report status after each step."
            ),
        }
    ]

    print(f"Starting pipeline for {tickers_str}, steps {steps_str}...\n")

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        # Collect assistant content blocks
        assistant_text_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                assistant_text_parts.append(block.text)
                print(block.text)
            elif block.type == "tool_use":
                tool_calls.append(block)

        # Append the full assistant message to history
        messages.append({"role": "assistant", "content": response.content})

        # If no tool calls, the agent is done
        if response.stop_reason == "end_turn" or not tool_calls:
            break

        # Execute each tool call and build results
        tool_results = []
        for tc in tool_calls:
            print(f"\n--- Executing: {tc.name} ---")
            handler = TOOL_DISPATCH.get(tc.name)
            if handler:
                result_text = handler(tc.input)
                # Print a brief status line
                first_line = result_text.split("\n")[0]
                print(first_line)
            else:
                result_text = f"Unknown tool: {tc.name}"
                print(result_text)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": result_text,
            })

        messages.append({"role": "user", "content": tool_results})

    print(f"\n{'='*60}")
    print("  Pipeline complete")
    print(f"{'='*60}")


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kinetic Revenue Agent — Managed Pipeline")
    parser.add_argument("--tickers", type=str, default=None,
                        help="Comma-separated tickers (default: all 5)")
    parser.add_argument("--steps", type=str, default=None,
                        help="Comma-separated step numbers (default: all 1-6)")
    args = parser.parse_args()

    tickers = args.tickers.split(",") if args.tickers else DEFAULT_TICKERS
    steps = [int(s) for s in args.steps.split(",")] if args.steps else None

    run_agent(tickers, steps)


if __name__ == "__main__":
    main()
