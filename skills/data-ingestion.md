# Data Ingestion

Covers `setup_db.py` and `ingest.py` — database schema creation and financial data ingestion from FinancialModelingPrep (FMP).

## Database Schema (`setup_db.py`)

Four tables, all on PostgreSQL (AWS RDS, SSL required):

### `revenue_actuals`
| Column | Type | Notes |
|--------|------|-------|
| id | SERIAL PK | |
| ticker | VARCHAR(10) | e.g. "SNOW" |
| period | VARCHAR(20) | Date string "YYYY-MM-DD", quarter-end date |
| revenue | BIGINT | Standalone quarterly revenue in USD |

Unique constraint on `(ticker, period)` — upserts via `ON CONFLICT DO UPDATE`.

### `consensus_estimates`
| Column | Type | Notes |
|--------|------|-------|
| id | SERIAL PK | |
| ticker | VARCHAR(10) | |
| period | VARCHAR(20) | Date string "YYYY-MM-DD" |
| estimated_revenue | BIGINT | Analyst consensus average revenue |

Unique constraint on `(ticker, period)`.

### `agent_reports`
| Column | Type | Notes |
|--------|------|-------|
| id | SERIAL PK | |
| ticker | VARCHAR(10) | |
| report | TEXT | Full Claude-generated research note |
| created_at | TIMESTAMP | Defaults to NOW() |

No unique constraint — each run appends a new report row.

### `transcripts`
| Column | Type | Notes |
|--------|------|-------|
| id | SERIAL PK | |
| ticker | VARCHAR(10) | |
| period | VARCHAR(20) | |
| transcript | TEXT | Earnings call transcript body |
| created_at | TIMESTAMP | Defaults to NOW() |

Unique constraint on `(ticker, period)`. Schema exists but no ingestion code yet — see [transcript-analysis.md](transcript-analysis.md).

## Ingestion Pipeline (`ingest.py`)

Entry point: `fetch_and_store()`, called at module level.

### Step 1 — Pull Quarterly Actuals

For each ticker, hits the FMP quarterly income statement endpoint:
```
GET /stable/income-statement?symbol={ticker}&period=quarter&limit=40
```
Extracts `date` and `revenue` from each quarter. Upserts into `revenue_actuals`.

**Important**: FMP returns up to 40 quarters (~10 years) of history per ticker.

### Step 2 — Fix Q4 Actuals (`fix_q4`)

This is the most critical data-quality step. **FMP stores fiscal-year cumulative revenue in the Q4 row**, not standalone Q4 revenue. This function corrects it:

1. Fetches annual income statements from FMP (`period=annual`, `limit=12`)
2. Loads all quarterly actuals for the ticker from the DB
3. For each annual row:
   - Finds the closest quarterly period within 45 days (this is the Q4 row)
   - Finds the 3 preceding quarters (30–400 days before Q4)
   - Computes: `Q4_standalone = annual_revenue - (Q1 + Q2 + Q3)`
   - Updates the DB if the corrected value differs and is positive

**Why this matters**: Without this fix, Q4 revenue would be ~4x too high, completely breaking QoQ analysis, seasonality, and projections.

### Step 3 — Rebuild Consensus Estimates (`rebuild_consensus`)

Rebuilds quarterly consensus from scratch each run:

1. **Delete** all existing estimates for the ticker (clean slate)
2. **Insert quarterly estimates** from FMP analyst estimates endpoint:
   ```
   GET /stable/analyst-estimates?symbol={ticker}&period=quarter
   ```
   Uses `revenueAvg` (analyst consensus average). These cover future periods where analysts have published estimates.
3. **Backfill historical periods**: For any quarter that has an actual but no analyst estimate, sets `consensus = actual`. This ensures the consensus table has a complete time series for charting and comparison.

### Data Flow Summary

```
FMP quarterly income stmt ──> revenue_actuals (raw, Q4 cumulative)
FMP annual income stmt ────> fix_q4() corrects Q4 in-place
FMP analyst estimates ─────> consensus_estimates (future quarters)
revenue_actuals ───────────> consensus_estimates (backfill historical = actuals)
```

### Error Handling

- If the FMP response is not a list (e.g. rate limit error, invalid ticker), prints a warning and skips
- All inserts use `ON CONFLICT DO UPDATE` so re-runs are idempotent
- The full rebuild of consensus (delete + re-insert) means stale estimates are cleaned up each run

### FMP API Notes

- Base URL: `https://financialmodelingprep.com/stable/`
- Auth: `apikey` query parameter from `FMP_API_KEY` env var
- Rate limits apply on free/starter plans — running all 49 tickers hits ~147 endpoints per run
- A 0.5-second delay is enforced between FMP API calls to avoid rate limiting
- Dates returned are quarter-end dates in `YYYY-MM-DD` format

## Ticker Universe (49 companies)

```
SNOW  DDOG  MDB   TENB  QLYS  NOW   ADBE  INTU  WDAY  PANW
CDNS  SNPS  ADSK  APP   FTNT  TEAM  VEEV  ROP   PLTR  HUBS
ZS    CRWD  OKTA  TTD   GTLB  BILL  MNDY  CFLT  ESTC  FROG
S     ZI    DT    TOST  PCTY  PAYC  GWRE  BSY   CWAN  NCNO
BRZE  KVYO  PCOR  SPSC  MANH  AZPN  APPN  DOCN
```

## Fiscal Year Calendar

Many software companies have non-calendar fiscal year-ends. Period matching
logic must account for this when joining transcripts to revenue_actuals.

The `FISCAL_CALENDAR` dict in ingest.py and transcript_ingest.py maps ticker
to fiscal year-end month. Default is 12 (calendar year). Tickers not listed
use calendar quarters.

### Non-Calendar Fiscal Year-Ends (24 tickers)
| FY End Month | Tickers |
|---|---|
| January (1) | SNOW, MDB, WDAY, VEEV, ADSK, OKTA, CRWD, GTLB, S, NCNO, BRZE, KVYO |
| March (3) | DT |
| April (4) | ESTC |
| June (6) | TEAM, BILL, PCTY, AZPN |
| July (7) | INTU, PANW, ZS, GWRE |
| October (10) | SNPS |
| November (11) | ADBE |

### Adding New Tickers
Before adding a new ticker, look up its fiscal year-end and add it to
FISCAL_CALENDAR in ingest.py and transcript_ingest.py. The period matching
logic will:
1. Check the fiscal calendar lookup for the ticker
2. If non-calendar FY, offset the quarter-end date accordingly
3. Match to the nearest revenue_actuals period within 90 days
4. Log a warning if no match found within 90 days — means ingest.py
   hasn't pulled data for that period yet

### Implementation Note
transcript_ingest.py was burned by this on first run — SNOW and MDB
transcripts were mapped to calendar quarter-ends (2026-12-31) instead of
fiscal quarter-ends (2026-01-31), breaking the join to revenue_actuals.
Fixed by expanding match window to 90 days. The explicit FISCAL_CALENDAR
lookup table is now the definitive source.