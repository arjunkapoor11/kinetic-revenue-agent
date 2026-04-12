# Dashboard

Covers `dashboard.py` and its output `dashboard.html` — the interactive revenue analysis dashboard.

## Overview

`dashboard.py` reads data from PostgreSQL, runs the same analytics pipeline as `agent.py` (QoQ, seasonality, anomalies, projections, consensus comparison), embeds the results as JSON into a self-contained HTML template, and opens it in the browser.

Entry point: `main()` at module level.

## Data Pipeline (`build_data`)

Iterates over all 5 tickers and for each:
1. Calls `get_db_data(ticker)` — same DB queries as `agent.py`
2. Runs `compute_qoq()`, `compute_seasonality()`, `flag_anomalies()`, `extrapolate()`, `consensus_comparison()`
3. Packages everything into a dict per ticker

The analytics functions in `dashboard.py` are **duplicated from `agent.py`** with minor differences:
- `flag_anomalies()` returns just the period strings (not full dicts) for simpler JS consumption
- `fy_totals()` is slightly more compact
- No Claude API call — the dashboard is purely data-driven

Returns: `{ "SNOW": {...}, "DDOG": {...}, ... }` — the full dataset for all tickers.

## HTML Generation (`generate_html`)

The HTML template is stored as a raw string constant `HTML_TEMPLATE` inside `dashboard.py`. Two placeholders are replaced at generation time:

- `__DATA__` → JSON-serialized analytics data (the full `all_data` dict)
- `__TIMESTAMP__` → Human-readable generation time ("April 10, 2026  23:45")

The resulting HTML is written to `dashboard.html` in the same directory and opened via `webbrowser.open()`.

## Dashboard UI Structure

### Design System
- Dark theme with CSS custom properties (`--bg-body: #090b10`, `--accent: #58a6ff`, etc.)
- Fonts: Inter (sans), JetBrains Mono (monospace for numbers)
- Both fonts rely on system fallbacks — not loaded from CDN
- Color coding: green (`--green: #34d399`) for beats/positive, red (`--red: #f87171`) for misses/negative, orange for consensus line, yellow for anomalies

### Layout (top to bottom)

**1. Header**
- Title: "Revenue Analysis Dashboard"
- Timestamp of generation

**2. Ticker Tabs**
- Horizontal tab bar, one button per ticker
- Clicking a tab re-renders the entire dashboard body for that ticker
- First ticker is selected by default on load

**3. Signal Cards** (4-column grid)
| Card | Source | Shows |
|------|--------|-------|
| Trailing YoY Growth | `avg_yoy` | Percentage, labeled "4-quarter average" |
| Next Quarter | `consensus.next_quarter` | BEAT/MISS signal with % gap, projection vs consensus in subtitle |
| Current FY | `consensus.current_fy` | BEAT/MISS with % gap, our total vs consensus total |
| Next FY | `consensus.next_fy` | BEAT/MISS with % gap, our total vs consensus total |

Cards get a colored left border and value color based on beat/miss/neutral status.

**4. Revenue Chart** (Chart.js line chart)
- **Actuals line**: solid white, filled area, last 20 quarters
- **Projection line**: dashed blue (`#58a6ff`), diamond point markers, continues from last actual
- **Consensus line**: dashed orange (`#fb923c`), no fill
- X-axis: time scale, quarter format ("Mar 25")
- Y-axis: revenue with `fmtRev()` formatting ($1.2B, $450.5M, etc.)
- Tooltip: index mode (shows all series on hover)

The projection line starts from the last actual data point to create visual continuity.

**5. Seasonality Row** (4-column grid)
- One card per quarter (Q1–Q4)
- Shows average QoQ dollar change (green/red colored)
- Subtitle: standard deviation and observation count

**6. Data Tables** (2-column grid)

**Historical Table (left):**
- Last 12 quarters of QoQ data
- Columns: Period, Quarter, Revenue, QoQ $, QoQ %
- Anomalous quarters highlighted with yellow left border and tinted background row

**Projection Table (right):**
- 8 forward quarters
- Columns: Period, Quarter, Projected, Consensus, Divergence %, Signal
- Each projection is matched to the nearest consensus estimate within 45 days
- Divergences >5% get a dot indicator (high-conviction signal)

## JavaScript Architecture

All rendering is client-side vanilla JS (no framework). Key functions:

- **`select(ticker)`** — switches active tab, calls `render()`
- **`render(ticker)`** — builds all HTML sections and the chart for the selected ticker
- **`buildCards(d)`** — generates signal card HTML
- **`buildSeason(s)`** — generates seasonality row HTML
- **`buildHistTable(d)`** — generates historical QoQ table HTML
- **`buildProjTable(d)`** — generates projection vs consensus table HTML, including the 45-day nearest-match logic for pairing projections with estimates
- **`buildChart(d)`** — creates/destroys Chart.js instance

### Formatters
- `fmtRev(v)` — revenue display: `$1.2B`, `$450.5M`, `$35K`, `$100`
- `fmtDelta(v)` — signed revenue: `+$50.2M` or `−$12.3M`
- `fmtPct(v)` — percentage: `+12.5%` or `-3.2%`
- `fmtDate(s)` — period display: "Mar 2025"
- `cls(v)` — returns CSS class `pos` / `neg` / empty
- `sigCls(s)` — returns `beat` / `miss` / `neutral`

## External Dependencies (CDN)

- `chart.js@4.4.7` — charting library
- `chartjs-adapter-date-fns@3.0.0` — date axis adapter

Both loaded from `cdn.jsdelivr.net`. The dashboard requires an internet connection for these scripts.

## Output

`dashboard.html` is a fully self-contained file (no server needed) except for the two CDN scripts. Data is embedded inline as a JS object. Can be shared as a single file — anyone with a browser can open it.

## Responsive Behavior

- At viewport widths below 1100px, the two-column table grid collapses to single column
- Chart and cards reflow naturally within the max-width 1440px container
