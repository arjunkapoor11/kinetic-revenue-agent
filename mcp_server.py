"""
Kinetic Revenue Agent — MCP Server

Standalone MCP server exposing 6 pipeline tools for the Anthropic Managed Agent.
Runs as an HTTP/SSE server. Reads credentials from AWS Secrets Manager in
production or .env for local development.

Usage:
    # Local development (reads from .env)
    python mcp_server.py

    # Production on AWS (reads from Secrets Manager)
    python mcp_server.py --secrets kinetic-revenue-agent --region us-east-2

    # Custom host/port
    python mcp_server.py --host 0.0.0.0 --port 3001
"""

import argparse
import io
import json
import os
import sys
import time
import traceback
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

PROJECT_DIR = Path(__file__).parent
DEFAULT_TICKERS = ["SNOW", "DDOG", "MDB", "TENB", "QLYS"]


# ── credential loading ─────────────────────────────────────────────────────

from credentials import load_credentials, add_credentials_args


# ── tool execution helper ──────────────────────────────────────────────────

def _run(fn) -> dict:
    """Run fn() capturing stdout/stderr and exceptions."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            fn()
        return {"ok": True, "out": out.getvalue(), "err": err.getvalue()}
    except Exception:
        return {"ok": False, "out": out.getvalue(), "err": err.getvalue(),
                "exc": traceback.format_exc()}


def _fmt(name: str, r: dict) -> str:
    """Format a tool result into a string for Claude."""
    status = "SUCCESS" if r["ok"] else "FAILED"
    parts = [f"[{name}] {status}"]
    if r["out"].strip():
        text = r["out"].strip()
        if len(text) > 4000:
            text = text[:2000] + "\n...(truncated)...\n" + text[-2000:]
        parts.append(f"Output:\n{text}")
    if r["err"].strip():
        parts.append(f"Stderr:\n{r['err'].strip()[:1000]}")
    if not r["ok"] and r.get("exc"):
        parts.append(f"Error:\n{r['exc'][:2000]}")
    return "\n".join(parts)


# ── create MCP server ─────────────────────────────────────────────────────

mcp = FastMCP(
    name="kinetic_pipeline",
    instructions=(
        "Kinetic Revenue Agent pipeline server. Provides 6 tools to ingest "
        "financial data, analyze transcripts, run quantitative analysis, "
        "generate dashboards, and export Excel models for SaaS companies."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


# ── tool definitions ───────────────────────────────────────────────────────

@mcp.tool()
def ingest_data(tickers: list[str] | None = None) -> str:
    """Step 1: Ingest quarterly revenue actuals and consensus estimates from
    FinancialModelingPrep API into PostgreSQL. Fixes Q4 cumulative data and
    rebuilds consensus estimates.

    Args:
        tickers: Ticker symbols to process (default: SNOW, DDOG, MDB, TENB, QLYS)
    """
    def run():
        sys.path.insert(0, str(PROJECT_DIR))
        import ingest
        ingest.TICKERS = list(tickers or DEFAULT_TICKERS)
        ingest.fetch_and_store()

    return _fmt("ingest_data", _run(run))


@mcp.tool()
def ingest_transcripts(tickers: list[str] | None = None) -> str:
    """Step 2: Fetch earnings call transcripts from Seeking Alpha (via RapidAPI)
    and store in PostgreSQL. Gets the last 4 transcripts per ticker.

    Args:
        tickers: Ticker symbols to process (default: all 5)
    """
    def run():
        sys.path.insert(0, str(PROJECT_DIR))
        import transcript_ingest
        transcript_ingest.TICKERS = list(tickers or DEFAULT_TICKERS)
        transcript_ingest.ingest_transcripts()

    return _fmt("ingest_transcripts", _run(run))


@mcp.tool()
def analyze_transcripts(ticker: str = "") -> str:
    """Step 3: Analyze earnings call transcripts for anomalous quarters using
    Claude for a SINGLE ticker. Call this tool once per ticker. Identifies
    management commentary explaining revenue distortions.

    Args:
        ticker: A single ticker symbol (e.g. "SNOW"). Required.
    """
    if not ticker:
        return "[analyze_transcripts] FAILED — ticker is required. Call once per ticker."

    def run():
        sys.path.insert(0, str(PROJECT_DIR))
        import transcript_analyzer
        transcript_analyzer.TICKERS = [ticker]
        transcript_analyzer.run_analysis()

    return _fmt(f"analyze_transcripts({ticker})", _run(run))


@mcp.tool()
def run_analysis(ticker: str = "") -> str:
    """Step 4: Run full quantitative analysis and generate a Claude research note
    for a SINGLE ticker. Call this tool once per ticker. Each call takes 60-90
    seconds. Saves the report to the agent_reports table.

    Args:
        ticker: A single ticker symbol (e.g. "SNOW"). Required.
    """
    if not ticker:
        return "[run_analysis] FAILED — ticker is required. Call once per ticker."

    def run():
        sys.path.insert(0, str(PROJECT_DIR))
        import agent as agent_mod
        agent_mod.run_agent(ticker)

    return _fmt(f"run_analysis({ticker})", _run(run))


@mcp.tool()
def generate_dashboard() -> str:
    """Step 5: Generate the interactive HTML dashboard (dashboard.html).
    Reads all data from PostgreSQL and writes a self-contained HTML file
    with Chart.js visualizations. No arguments required.
    """
    def run():
        sys.path.insert(0, str(PROJECT_DIR))
        from dashboard import build_data, generate_html
        data = build_data()
        html = generate_html(data)
        path = str(PROJECT_DIR / "dashboard.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Dashboard written to {path}")

    return _fmt("generate_dashboard", _run(run))


@mcp.tool()
def export_to_excel() -> str:
    """Step 6: Export the revenue model to kinetic_revenue_model.xlsx.
    Professional financial model with wide time-series layout, live Excel
    formulas, guide inference, and beat history. No arguments required.
    """
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

    return _fmt("export_to_excel", _run(run))


@mcp.tool()
def earnings_prep(ticker: str = "") -> str:
    """Fetch raw earnings preparation data for a ticker from RDS.
    Returns structured JSON with revenue estimates, consensus, beat cadence,
    recent anomalies, and transcript analyses. The agent should use this data
    to synthesize a comprehensive earnings prep document.

    Args:
        ticker: A single ticker symbol (e.g. "SNOW"). Required.
    """
    if not ticker:
        return "[earnings_prep] FAILED — ticker is required."

    def run():
        sys.path.insert(0, str(PROJECT_DIR))
        from earnings_prep import fetch_earnings_data
        data = fetch_earnings_data(ticker)
        print(json.dumps(data, indent=2))

    return _fmt(f"earnings_prep({ticker})", _run(run))


@mcp.tool()
def run_x_sentiment(date: str = "", resend: bool = False) -> str:
    """Run the X (Twitter) developer sentiment tracker and return the daily
    summary. Searches X for developer posts about AI model providers (Anthropic,
    OpenAI, Google, Meta, xAI, Mistral, DeepSeek, Qwen), analyzes sentiment
    with Claude, and returns aggregated results.

    Args:
        date: Optional date in YYYY-MM-DD format. Defaults to today.
              If data already exists for that date, returns cached summary
              without re-fetching.
        resend: If True, send the email report even if one was already
                     sent today. Useful for regenerating reports after data
                     changes.
    """
    def run():
        import psycopg2
        sys.path.insert(0, str(PROJECT_DIR))
        from x_sentiment_tracker import (
            get_daily_summary, run_pipeline, send_email_summary,
            get_db_connection, reload_email_prompt,
        )
        from datetime import datetime as dt, timezone

        target = date.strip() if date else None
        target_date = (
            dt.strptime(target, "%Y-%m-%d").date()
            if target else dt.now(timezone.utc).date()
        )

        # Check if we already have a summary for this date
        summary = get_daily_summary(target)
        if summary["total_posts"] > 0:
            print(json.dumps(summary, indent=2, default=str))

            # Send email unless already sent (resend overrides)
            conn = get_db_connection()
            should_send = resend

            if not should_send:
                cur = conn.cursor()
                cur.execute(
                    "SELECT 1 FROM x_email_reports WHERE date = %s",
                    (target_date,),
                )
                should_send = cur.fetchone() is None
                cur.close()

            if should_send:
                if resend:
                    reload_email_prompt()
                    print("[email] Regenerating report (resend — skills file reloaded)")
                else:
                    print("[email] Sending report (not yet sent)")
                send_email_summary(conn, summary["by_provider"], target_date)
            else:
                print("[email] Report already sent for this date")

            conn.close()
            return

        # No cached data — run the full pipeline
        run_pipeline(test_mode=False)
        summary = get_daily_summary(target)
        print(json.dumps(summary, indent=2, default=str))

        # Send email after pipeline run
        if summary["total_posts"] > 0:
            conn = get_db_connection()
            send_email_summary(conn, summary["by_provider"], target_date)
            conn.close()

    return _fmt("run_x_sentiment", _run(run))


@mcp.tool()
def manage_distribution_list(action: str, email: str = "", name: str = "") -> str:
    """Manage the X sentiment report email distribution list.

    Args:
        action: One of "add", "remove", or "list".
        email: Email address (required for add/remove, ignored for list).
        name: Display name (optional, used with add).

    Examples:
        manage_distribution_list(action="add", email="chris@kinetic.com", name="Chris")
        manage_distribution_list(action="remove", email="old@kinetic.com")
        manage_distribution_list(action="list")
    """
    def run():
        import psycopg2
        sys.path.insert(0, str(PROJECT_DIR))
        from credentials import load_credentials
        load_credentials()

        conn = psycopg2.connect(
            host=os.environ.get("DB_HOST"),
            database=os.environ.get("DB_NAME"),
            user=os.environ.get("DB_USER"),
            password=os.environ.get("DB_PASSWORD"),
            port=5432, sslmode="require",
        )
        cur = conn.cursor()

        if action == "add":
            if not email:
                print("ERROR: email is required for add action")
                return
            cur.execute("""
                INSERT INTO x_distribution_list (email, name, active)
                VALUES (%s, %s, TRUE)
                ON CONFLICT (email)
                DO UPDATE SET name = EXCLUDED.name, active = TRUE
            """, (email.strip().lower(), name.strip()))
            conn.commit()
            print(f"Added {email} to distribution list")

        elif action == "remove":
            if not email:
                print("ERROR: email is required for remove action")
                return
            cur.execute(
                "UPDATE x_distribution_list SET active = FALSE WHERE email = %s",
                (email.strip().lower(),),
            )
            conn.commit()
            if cur.rowcount:
                print(f"Removed {email} from distribution list")
            else:
                print(f"{email} not found in distribution list")

        elif action == "list":
            cur.execute(
                "SELECT email, name, active, added_at "
                "FROM x_distribution_list ORDER BY added_at"
            )
            rows = cur.fetchall()
            if not rows:
                print("Distribution list is empty")
            else:
                print(f"{'Email':<35s} {'Name':<20s} {'Active':<8s} Added")
                print("-" * 80)
                for r in rows:
                    status = "yes" if r[2] else "no"
                    print(f"{r[0]:<35s} {(r[1] or ''):<20s} {status:<8s} "
                          f"{r[3].strftime('%Y-%m-%d') if r[3] else 'N/A'}")
                active = sum(1 for r in rows if r[2])
                print(f"\n{active} active, {len(rows) - active} inactive")
        else:
            print(f"ERROR: Unknown action '{action}'. Use add, remove, or list.")

        cur.close()
        conn.close()

    return _fmt("manage_distribution_list", _run(run))


@mcp.tool()
def post_to_slack() -> str:
    """Step 7: Post pipeline summary to Slack #software-dashboard channel.
    Reads the most recent guide inference signals from the database and
    posts a formatted summary with GUIDE ABOVE / GUIDE BELOW signals.
    No arguments required.
    """
    def run():
        sys.path.insert(0, str(PROJECT_DIR))
        from slack_notify import build_and_post
        build_and_post()

    return _fmt("post_to_slack", _run(run))


# ── main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kinetic Revenue Agent MCP Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=3001, help="Port (default: 3001)")
    add_credentials_args(parser)
    args = parser.parse_args()

    load_credentials(secret_name=args.secrets, region=args.region)

    # Configure server host/port
    mcp.settings.host = args.host
    mcp.settings.port = args.port

    print(f"[server] Starting MCP server on {args.host}:{args.port}")
    print(f"[server] Streamable HTTP endpoint: http://{args.host}:{args.port}/mcp")
    print(f"[server] Tools: ingest_data, ingest_transcripts, analyze_transcripts, "
          f"run_analysis, generate_dashboard, export_to_excel, run_x_sentiment, "
          f"manage_distribution_list, post_to_slack")

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
