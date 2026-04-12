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

def load_from_env():
    """Load credentials from .env file (local development)."""
    from dotenv import load_dotenv
    load_dotenv(PROJECT_DIR / ".env")
    print("[credentials] Loaded from .env")


def load_from_secrets_manager(secret_name: str, region: str):
    """Load credentials from AWS Secrets Manager (production).

    Expected secret JSON keys: DB_HOST, DB_NAME, DB_USER, DB_PASSWORD,
    FMP_API_KEY, ANTHROPIC_API_KEY, RAPIDAPI_KEY
    """
    import boto3

    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_name)
    secrets = json.loads(response["SecretString"])

    for key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD",
                "FMP_API_KEY", "ANTHROPIC_API_KEY", "RAPIDAPI_KEY", "SLACK_WEBHOOK"):
        if key in secrets:
            os.environ[key] = secrets[key]

    print(f"[credentials] Loaded from Secrets Manager: {secret_name} ({region})")


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
    parser.add_argument("--secrets", default=None,
                        help="AWS Secrets Manager secret name (omit for .env)")
    parser.add_argument("--region", default="us-east-2",
                        help="AWS region (default: us-east-2)")
    args = parser.parse_args()

    # Load credentials
    if args.secrets:
        load_from_secrets_manager(args.secrets, args.region)
    else:
        load_from_env()

    # Configure server host/port
    mcp.settings.host = args.host
    mcp.settings.port = args.port

    print(f"[server] Starting MCP server on {args.host}:{args.port}")
    print(f"[server] Streamable HTTP endpoint: http://{args.host}:{args.port}/mcp")
    print(f"[server] Tools: ingest_data, ingest_transcripts, analyze_transcripts, "
          f"run_analysis, generate_dashboard, export_to_excel, post_to_slack")

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
