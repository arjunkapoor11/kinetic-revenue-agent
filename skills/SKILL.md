# Revenue Agent — Skill Index

This project is a **quarterly revenue analysis system** for high-growth SaaS companies. It ingests financial data from external APIs, stores it in PostgreSQL, runs quantitative analysis with Claude-generated research notes, and renders an interactive HTML dashboard.

## Covered Tickers

SNOW (Snowflake), DDOG (Datadog), MDB (MongoDB), TENB (Tenable), QLYS (Qualys)

Defined in the `TICKERS` list at the top of `ingest.py`, `agent.py`, and `dashboard.py`.

## Architecture

```
FMP API ──> ingest.py ──> PostgreSQL ──> agent.py ──> Claude API ──> agent_reports table
                              │
                              └──────> dashboard.py ──> dashboard.html (static file, opens in browser)
```

## Pipeline Execution Order

1. **`setup_db.py`** — Run once to create tables (`revenue_actuals`, `consensus_estimates`, `agent_reports`, `transcripts`)
2. **`ingest.py`** — Pulls quarterly actuals + consensus estimates from FinancialModelingPrep, fixes Q4 data, writes to DB
3. **`agent.py`** — Reads DB, computes analytics (QoQ, seasonality, anomalies, projections, consensus comparison), sends to Claude for a research note, saves report to DB
4. **`dashboard.py`** — Reads DB, computes the same analytics client-side, generates a self-contained HTML dashboard with Chart.js, opens in browser

## Sub-Skills

| File | Covers |
|------|--------|
| [data-ingestion.md](data-ingestion.md) | `setup_db.py`, `ingest.py`, DB schema, FMP API, Q4 fix, consensus rebuild |
| [analysis-framework.md](analysis-framework.md) | `agent.py` — QoQ, seasonality, anomaly detection, extrapolation, consensus comparison, Claude prompt |
| [transcript-analysis.md](transcript-analysis.md) | `transcripts` table, planned earnings call transcript ingestion and analysis |
| [dashboard.md](dashboard.md) | `dashboard.py`, `dashboard.html` — data pipeline, HTML generation, Chart.js, UI components |
| [output-formatting.md](output-formatting.md) | `export.py`, `kinetic_revenue_model.xlsx` — Excel model formatting standards, wide time-series layout |
| [managed-agents.md](managed-agents.md) | `managed_agent.py` — local orchestrator, tool definitions, system prompt, error handling |
| [deployment.md](deployment.md) | `mcp_server.py`, `deploy_agent.py` — Managed Agents deployment, MCP server, AWS, credential vaulting |

## Environment

- **Runtime**: Python 3, no virtual environment currently configured
- **Database**: PostgreSQL on AWS RDS (us-east-2), SSL required
- **APIs**: FinancialModelingPrep (FMP) for financial data, Anthropic Claude API for LLM analysis, RapidAPI key present (for future transcript sourcing)
- **Config**: `.env` file with `FMP_API_KEY`, `ANTHROPIC_API_KEY`, `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `RAPIDAPI_KEY`

## Key Dependencies

- `psycopg2` — PostgreSQL driver
- `anthropic` — Claude API client
- `requests` — HTTP client for FMP API
- `python-dotenv` — .env loading
- `Chart.js` + `chartjs-adapter-date-fns` — loaded via CDN in the dashboard HTML

## Utility Files

- **`debug.py`** — Minimal script that sends "Say hello" to Claude. Used to verify the Anthropic API key works.
