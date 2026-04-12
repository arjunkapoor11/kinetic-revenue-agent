# Transcript Analysis

Covers the planned earnings call transcript ingestion and analysis capability. The database schema exists but no ingestion or analysis code has been written yet.

## Current State

### What exists:
- **`transcripts` table** in PostgreSQL (created by `setup_db.py`):

| Column | Type | Notes |
|--------|------|-------|
| id | SERIAL PK | |
| ticker | VARCHAR(10) | e.g. "SNOW" |
| period | VARCHAR(20) | Quarter-end date "YYYY-MM-DD" |
| transcript | TEXT | Full earnings call transcript |
| created_at | TIMESTAMP | Defaults to NOW() |

Unique constraint on `(ticker, period)` — one transcript per ticker per quarter.

- **`RAPIDAPI_KEY`** in `.env` — likely intended for a transcript data provider (e.g. Seeking Alpha, Earnings Call Transcript API on RapidAPI)

### What doesn't exist yet:
- No transcript ingestion code (no fetch from any API)
- No transcript analysis/parsing logic
- No integration with the agent or dashboard

## Intended Purpose

Earnings call transcripts contain forward-looking commentary from management that can supplement the quantitative revenue analysis:

- **Revenue guidance** — management's own revenue outlook for upcoming quarters
- **Segment commentary** — which product lines are accelerating or decelerating
- **Customer metrics** — net revenue retention, customer count, expansion signals
- **Macro commentary** — how management characterizes demand environment
- **Competitive positioning** — mentions of competitive wins/losses, market share

## Fiscal Calendar Handling

Both `ingest.py` and `transcript_ingest.py` define a `FISCAL_CALENDAR` dictionary mapping tickers to their fiscal year-end month. Companies not in the dictionary default to month 12 (calendar year).

```python
FISCAL_CALENDAR = {
    "SNOW": 1,  # Snowflake: FY ends January 31
    "MDB": 1,   # MongoDB:   FY ends January 31
}
```

`transcript_ingest.py` uses `fiscal_quarter_end(ticker, fy_year, fy_quarter)` to convert a fiscal quarter label (e.g. "Q4 FY2026") into the correct calendar date (e.g. `2026-01-31` for SNOW, `2026-12-31` for DDOG). Adding a new non-calendar-FY company requires only one line in the dictionary.
