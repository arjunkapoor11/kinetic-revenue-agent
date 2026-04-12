# Analysis Framework

Covers `agent.py` — the quantitative analysis engine and Claude-powered research note generator.
## Changelog

### 2026-04-12
- Q+1 estimate: consensus × (1 + avg beat %) — anchored to sell-side
- Q+2-Q+4: $ QoQ decision tree chained off beat-adjusted Q+1
- Implied guide: Q+2 only — Q+2 estimate ÷ (1 + avg beat %)
- Excel: complete rewrite with hardcoded-vs-formula separation (see below)
- Excel: added Consensus Revenue section with full memo lines
- Excel: transcript anomaly cell comments, beat math comments on forward quarters

### 2026-04-11 (initial build)
- $ QoQ decision tree framework
- Beat cadence 4Q/8Q window
- Guide inference GUIDE BELOW/IN-LINE/ABOVE signals
- Transcript anomaly detection and classification
- Cloud deployment: EC2 + RDS + Anthropic Managed Agents

---------------------------

## Overview

`agent.py` iterates over all 5 tickers, pulls data from PostgreSQL, computes a suite of revenue analytics using the **Kinetic $ QoQ forecasting methodology**, packages them into a structured JSON payload, sends it to Claude with a hedge-fund analyst prompt, and saves the resulting research note to the `agent_reports` table.

Entry point: the `for ticker in TICKERS: run_agent(ticker)` loop at module level.

## Core Principle: $ QoQ as the Primary Metric

**Dollar quarter-over-quarter change ($ QoQ) is the primary forecasting metric for recurring revenue SaaS businesses — not % YoY.**

Why $ QoQ over % YoY:
- SaaS revenue is recurring, so sequential adds compound on a growing base
- % YoY compresses mechanically as the base grows, creating a misleading deceleration narrative
- $ QoQ directly measures how much net-new revenue the business is adding each quarter
- Seasonal patterns in $ QoQ are more stable and forecastable than % growth rates

## Analytics Pipeline

```
get_db_data() → compute_qoq() → compute_seasonality() → flag_anomalies()
                      │
                      ├──> classify_seasonal_trend() (per Q1/Q2/Q3/Q4)
                      ├──> compute_momentum() (last 2-3 quarters)
                      ├──> compute_qoq_yoy() (YoY change on $ QoQ)
                      ├──> extrapolate() → consensus_comparison()
                      │
               compute_beat_cadence(actuals, estimates)
                      │
                      └──> build_guide_inference() → implied guide signals
```

### 1. `get_db_data(ticker)`

Reads from PostgreSQL:
- `revenue_actuals` — all quarterly periods + revenue, ordered by period ASC
- `consensus_estimates` — all quarterly periods + estimated_revenue, ordered by period ASC

Returns two lists of dicts: `actuals` and `estimates`.

### 2. `compute_qoq(actuals)`

Computes quarter-over-quarter revenue changes for every consecutive pair of quarters.

For each quarter `i` (starting from the second):
- `dollar_change = current_revenue - previous_revenue`
- `pct_change = dollar_change / previous_revenue * 100`
- Tags each row with its calendar quarter (Q1–Q4) via `quarter_from_date()`

Returns a list of dicts with: `period`, `quarter`, `revenue`, `prev_revenue`, `qoq_dollar_change`, `qoq_pct_change`.

### 3. `compute_seasonality(qoq_data)`

Groups all QoQ dollar changes by calendar quarter (Q1, Q2, Q3, Q4) and computes:
- **`avg_qoq_change`**: Mean QoQ dollar change for that quarter across all years
- **`std_qoq_change`**: Standard deviation (0 if only one observation)
- **`observations`**: Sample size

Used for anomaly detection and as a reference. **Not used directly for projection** — the seasonal trend classifier handles that.

### 4. `flag_anomalies(qoq_data, seasonal)`

Flags quarters where the QoQ dollar change deviates more than **1.5 standard deviations** from the seasonal average:

```
deviation = |actual_qoq_change - seasonal_avg| / seasonal_std
if deviation > 1.5 → flagged as anomaly
```

Returns a list of dicts with: `period`, `quarter`, `actual_qoq_change`, `seasonal_avg`, `std_deviations`.

### 5. `classify_seasonal_trend(values)` — The Decision Tree

For each seasonal quarter (Q1/Q2/Q3/Q4), takes the chronological list of historical $ QoQ values and classifies the trend. This is the core of the Kinetic methodology.

**Decision tree:**

| Trend | Condition | Projection |
|-------|-----------|------------|
| **Flat** | Coefficient of variation < 10% | Hold last datapoint |
| **Accelerating** | ≥60% of diffs positive AND recent 2nd derivative positive | Slightly increase last datapoint (+3%) |
| **Decelerating** | ≥60% of diffs positive AND recent 2nd derivative negative | Apply modest haircut to last datapoint (−8%) |
| **Growing** | ≥60% of diffs positive, no clear acceleration pattern | Project off last datapoint (not the average) |
| **Declining** | ≥60% of diffs negative | Extrapolate the decline (last + avg recent decline) |
| **Volatile/Mixed** | None of the above | Recency-weighted average (weights: 1, 2, 3, ... for oldest to newest) |

**Key insight**: for consistently growing $ QoQ, use the **last datapoint** as the projection base, not the historical average. The average understates the current trajectory. The decision tree then adjusts up or down depending on acceleration/deceleration.

**Classification logic in detail:**

1. **Flat check first** — if the coefficient of variation (std/mean) of all values is < 10%, the series is flat regardless of other patterns. Project off the last value.

2. **Growth classification** — count the proportion of positive sequential diffs:
   - ≥60% positive → growing trend. Then check the last 2-3 diffs for acceleration (2nd derivative):
     - All positive → **accelerating** (growth of growth is increasing)
     - All negative → **decelerating** (growth of growth is decreasing)
     - Mixed → **growing** (steady uptrend without clear acceleration signal)

3. **Decline classification** — ≥60% negative sequential diffs → **declining**. Project using last value plus the average of the most recent 2-3 diffs (extrapolating the decline).

4. **Volatile fallback** — if neither growing nor declining dominates, use a recency-weighted average where more recent observations carry proportionally more weight.

### 6. `compute_momentum(qoq_data)` — The Momentum Overlay

Analyzes the **last 2-3 quarters of $ QoQ regardless of seasonality** to determine if the business is accelerating or decelerating in the near term.

This is a cross-seasonal signal — it ignores Q1/Q2/Q3/Q4 identity and just looks at the raw trajectory of recent sequential adds.

| Signal | Condition | Factor |
|--------|-----------|--------|
| Accelerating | All recent diffs positive | ×1.03 (+3% nudge) |
| Decelerating | All recent diffs negative | ×0.97 (−3% nudge) |
| Stable | Mixed | ×1.00 (no adjustment) |

The momentum factor is multiplied into each quarter's projected $ QoQ before adding it to the prior quarter's revenue.

**Why this matters**: Seasonal analysis can miss inflection points. If a company just had two consecutive quarters where $ QoQ improved (e.g., $40M → $50M → $55M adds), the business has short-term positive momentum that should be reflected in projections even if the seasonal history for the next quarter doesn't capture it.

### 7. `compute_qoq_yoy(qoq_data)` — YoY Change on $ QoQ

For fast-growing names, computes how $ QoQ is changing year-over-year:

```
For each quarter with a year-ago comp:
  yoy_change = current_qoq - prior_year_qoq
  yoy_pct = yoy_change / |prior_year_qoq| * 100
```

This contextualizes the growth rate: if a company added $50M in Q3 last year and $65M in Q3 this year, that's a +30% YoY improvement in $ QoQ — the business is scaling its sequential adds, a very bullish signal.

Used in the Claude prompt for narrative context, not directly in the projection math.

### 8. `extrapolate(actuals, qoq_data, estimates, beat_cadence, n=4)` — Forward Projection

Projects revenue for the **next 4 quarters** using a two-tier methodology:

**Q+1 (next reported quarter):**
- Uses consensus estimate as guide proxy
- `Q+1 revenue = consensus × (1 + avg beat %)`
- Anchored to sell-side estimates, not pure extrapolation

**Q+2 through Q+4:**
- Chains $ QoQ decision tree off the beat-adjusted Q+1
- For each quarter: look up seasonal forecast, apply momentum overlay
- `projected_revenue = prior_quarter_revenue + momentum-adjusted $ QoQ`

Falls back to pure $ QoQ for all quarters if no beat cadence or Q+1 consensus available.

**Return values**: `(projections, seasonal_forecasts, momentum_label, momentum_factor)`

**Projection chaining**: each quarter builds on the prior, so errors compound. This is why we only project 4 quarters (not 8) — beyond 4Q, the uncertainty grows substantially.

### 9. `consensus_comparison(actuals, projections, estimates)`

Compares the model's projections against Wall Street consensus at three levels:

**Next Quarter:**
- Takes the first projection period
- Finds the closest consensus estimate within 45 days
- Computes dollar and percentage divergence
- Labels as `BEAT` or `MISS`

**Current FY and Next FY:**
- Sums actuals + projections for the year → "our total"
- Sums consensus estimates for the year → "consensus total"
- Computes divergence and signal
- Tracks how many quarters are actual vs. projected

Returns: `(next_q_comparison, current_fy_totals, next_fy_totals)`

### 10. `compute_beat_cadence(actuals, estimates)` — Beat Cadence

Measures how consistently a company beats or misses consensus estimates, and by how much.

**Data source:**
- Pulls from the FMP `/stable/earnings` endpoint, which returns historical `revenueActual` and `revenueEstimated` (pre-earnings consensus) for each earnings event
- This is the only reliable source for beat data — the `consensus_estimates` table has backfilled historical periods where estimate == actual (see [data-ingestion.md](data-ingestion.md))
- For each record: `beat_pct = (revenueActual - revenueEstimated) / revenueEstimated * 100`
- FMP returns records newest-first; the function takes the last 4 and last 8 records

**Window selection:**
- Computes average beat % over the **last 4 quarters** and **last 8 quarters**
- Computes standard deviation for each window
- If the two averages diverge by more than **1.5 percentage points**, flags as `is_changing` — the beat pattern is shifting (e.g., company was sandbagging more and is now guiding tighter, or vice versa)
- Selects the window with **lower standard deviation** (more stable/consistent beats) as the canonical `selected_beat_pct`

**Example output:**
```json
{
  "avg_beat_4q": 2.3,
  "avg_beat_8q": 1.8,
  "std_4q": 0.5,
  "std_8q": 1.2,
  "selected_beat_pct": 2.3,
  "selected_window": "4Q",
  "is_changing": false
}
```

A `selected_beat_pct` of +2.3% means the company typically prints actual revenue 2.3% above consensus. A negative value means systematic misses.

### 11. `build_guide_inference(projections, beat_cadence)` — Implied Q+2 Guide

Computes the implied guide for **Q+2 only** — the first unguided quarter:

```
implied_guide = Q+2 projected actual / (1 + selected_beat_pct / 100)
```

This reverses the beat cadence to infer what management will guide to, assuming they maintain their historical sandbagging/accuracy pattern. Only Q+2 is modeled because Q+1 is already guided (consensus reflects the existing guide).

**Signal Generation:**
- Compares implied guide to Wall Street consensus for Q+2

| Signal | Condition | Meaning |
|--------|-----------|---------|
| **GUIDE ABOVE** | implied guide > consensus by >2% | Bullish — management will likely raise above Street |
| **GUIDE IN-LINE** | implied guide within ±2% of consensus | No surprise expected |
| **GUIDE BELOW** | implied guide < consensus by >2% | Bearish — management will likely guide below Street |

**Return structure:**
```json
{
  "period": "2026-09-30",
  "quarter": "Q3",
  "projected_actual": 1180000000,
  "implied_guide": 1153000000,
  "consensus": 1140000000,
  "gap_dollars": 13000000,
  "gap_pct": 1.14,
  "signal": "GUIDE IN-LINE",
  "beat_cadence": { ... }
}
```

## Claude Prompt

The full analytics payload is serialized to JSON and embedded in a prompt that instructs Claude (claude-sonnet-4-6, max 3500 tokens) to write a research note with these sections:

1. **$ QoQ Revenue Trend** — recent $ QoQ changes, momentum, acceleration/deceleration
2. **Seasonal $ QoQ Analysis** — per-quarter trend classification, projected $ QoQ, strongest/weakest
3. **Momentum Overlay** — cross-seasonal momentum signal interpretation
4. **YoY Change in $ QoQ** — how sequential adds are scaling (for fast growers)
5. **Anomalous Quarters** — likely drivers, whether to weight in forward estimates
6. **4-Quarter Forward Projection** — table with period, quarter, projected revenue, projected $ QoQ, trend, momentum
7. **Beat Cadence & Guide Inference** — historical beat %, beat-adjusted current quarter, 4-quarter implied guide path, flag quarters where implied guide diverges materially from consensus
8. **Consensus Comparison** — beat/miss at next-quarter, current FY, next FY; flags >5% divergences as high-conviction
9. **Investment Implication** — 2–3 sentences on positioning with specific numbers

The prompt persona is "senior financial analyst at a hedge fund covering high-growth software companies." The prompt also explains the Kinetic $ QoQ methodology and beat cadence framework so Claude can interpret the data correctly.

## Output

The Claude-generated report is:
1. Printed to stdout with a formatted header
2. Inserted into `agent_reports` table with the ticker and current timestamp

## Helper Functions

- **`quarter_from_date(date_str)`**: Extracts calendar quarter (1–4) from a "YYYY-MM-DD" date string
- **`quarter_end_date(year, quarter)`**: Returns the last day of the quarter as "YYYY-MM-DD" (e.g., Q1 → "2025-03-31")
- **`get_db_connection()`**: Returns a psycopg2 connection using env vars, SSL required

## Key Design Decisions

- **$ QoQ is primary, not % YoY**: Dollar sequential adds are the native unit of SaaS forecasting. Percentage growth compresses mechanically on a growing base and is misleading for projection.
- **Last datapoint over average**: For growing trends, the most recent $ QoQ for a seasonal quarter is a better projection base than the historical average, which lags the current trajectory.
- **Momentum overlay is cross-seasonal**: Seasonal analysis can miss inflection points. The 2-3 quarter momentum overlay captures short-term shifts that haven't yet shown up in the seasonal history for a specific quarter.
- **4 quarters, not 8**: Shorter projection horizon reduces compounding error from chained $ QoQ. 4 quarters covers the actionable investment horizon.
- **Decision tree, not regression**: The trend classification is intentionally simple and interpretable. Each forecast has a clear label (growing, accelerating, etc.) that an analyst can audit and override.
- **1.5 sigma anomaly threshold**: Relatively sensitive — catches moderate outliers, not just extreme ones. Good for flagging quarters worth investigating.
- **Consensus backfill**: Historical quarters use actual revenue as "consensus" (see [data-ingestion.md](data-ingestion.md)), so consensus comparison is only meaningful for future/recent quarters where real analyst estimates exist.
- **Beat cadence uses FMP earnings endpoint, not consensus_estimates table**: The `/stable/earnings` endpoint provides historical `revenueActual` vs `revenueEstimated` pairs — the only FMP source that preserves pre-earnings consensus after a quarter is reported. The `consensus_estimates` table backfills historical periods (estimate = actual), making it useless for beat computation.
- **Beat cadence uses the more stable window**: Between 4Q and 8Q, the window with lower standard deviation is selected. A company that has beaten by exactly 2% for 4 straight quarters is a stronger signal than one that alternated between 0% and 4% over 8 quarters, even if the averages are similar.
- **1.5 pp divergence threshold for changing beat pattern**: If 4Q and 8Q averages differ by more than 1.5 percentage points, the beat pattern is flagged as changing. This catches shifts in management's guidance philosophy (tighter guides, strategy changes, etc.).
- **Implied guide inverts the beat formula**: `guide = actual / (1 + beat_pct)` is the algebraic inverse of `actual = guide * (1 + beat_pct)`. This assumes management maintains their historical sandbagging/accuracy pattern. The ±2% threshold for GUIDE ABOVE/IN-LINE/BELOW avoids noise from small rounding differences.
- **Guide inference is the primary dashboard signal**: The guide inference table is the most prominent element of the dashboard because it synthesizes the full framework ($ QoQ + beat cadence) into an actionable earnings preview.
- **Q+2 guide only, not Q+1-Q+4**: Q+1 is already guided (consensus reflects management's existing guide). Projecting implied guides for Q+3/Q+4 compounds uncertainty beyond usefulness. Q+2 is the actionable signal — the first unguided quarter.

## Excel Output Standards (`export.py`)

### Hardcoded vs Formula Rules

**Hardcoded (written as numbers):**
| Cell | Font | Source |
|------|------|--------|
| Historical total revenue | Black, bold | revenue_actuals table |
| Forward $ QoQ driver | Blue, italic | $ QoQ decision tree output (the assumption input) |
| Forward consensus revenue | Black, bold | consensus_estimates table |

**Formula-driven (must be Excel formulas):**
| Cell | Formula |
|------|---------|
| Forward total revenue | `= prior Q revenue + $ QoQ driver` |
| Historical consensus revenue | `= total revenue cell` (same number, formula ref) |
| % QoQ | `= (this Q - prior Q) / prior Q` |
| % YoY | `= (this Q - same Q prior year) / same Q prior year` |
| $ QoQ (historical) | `= this Q revenue - prior Q revenue` |
| $ YoY | `= this Q revenue - same Q prior year revenue` |
| % Variance vs Consensus | `= (revenue - consensus) / consensus` |
| Implied Q+2 Guide | `= Q+2 revenue / (1 + beat cadence %)` |
| % Variance Guide vs Consensus | `= (guide - consensus) / consensus` |
| FY totals | `= SUM(Q1:Q4)` for both revenue and consensus |
| Consensus memo lines | All formulas referencing consensus revenue row |

### Row Structure Per Ticker Sheet

```
Row 1:  TICKER: KIN Base Case Operating Model  [bold]
Row 2:  USD in Millions Unless Stated Otherwise  [italic]
Row 3:  Beat Cadence (%)  [label + blue value]
Row 4:  Beat Window  [label + blue value]
Row 5:  Momentum  [label + blue value]
Row 6:  [empty]
Row 7:  Period headers  (Q1-24, Q2-24, ... FY24, Q1-25E, ...)
Row 8:  [empty]
Row 9:  Total Revenue  [bold, $M 1dp, hardcoded actual / formula forward]
Row 10: % YoY  [italic, formula]
Row 11: % QoQ  [italic, formula]
Row 12: $ YoY  [italic, formula, $M 1dp]
Row 13: $ QoQ  [italic, blue text + hardcoded for forward, formula for historical]
Row 14: [empty]
Row 15: Consensus Total Revenue  [bold, hardcoded forward, formula = actuals historical]
Row 16: % YoY  [italic, formula ref consensus row]
Row 17: % QoQ  [italic, formula ref consensus row]
Row 18: $ YoY  [italic, formula ref consensus row]
Row 19: $ QoQ  [italic, formula ref consensus row]
Row 20: [empty]
Row 21: % Variance vs Consensus  [italic, formula, green/red text, forward only]
Row 22: [empty]
Row 23: Implied Q+2 Guide  [bold, one column, formula = rev / (1 + beat%)]
Row 24: % YoY  [italic, formula]
Row 25: % QoQ  [italic, formula]
Row 26: $ YoY  [italic, formula]
Row 27: $ QoQ  [italic, formula]
Row 28: [empty]
Row 29: % Variance Guide vs Consensus  [italic, formula, green/red, one column]
```

### Column Structure

- Wide time-series left to right, grouped by fiscal year: Q1 | Q2 | Q3 | Q4 | FY
- Medium-weight borders on left of Q1 and right of FY for each year group
- FY column = SUM(Q1:Q4) for both revenue and consensus rows
- Column headers: "Q{n}-{YY}" for actuals, "Q{n}-{YY}E" for estimates

### Formatting

- Font: Times New Roman throughout
- No gridlines
- Revenue: `#,##0.0` in $M, parentheses for negatives, `"-"` for zeros
- Percentages: `0.0%`, parentheses for negatives, `"-"` for zeros
- Section headers (Total Revenue, Consensus, Guide): bold, not italic
- Sub-rows (% YoY, % QoQ, etc.): italic
- $ QoQ driver (forward): italic blue — this is the key assumption input
- Variance rows: italic green text if positive, italic red text if negative
- Beat cadence, beat window, momentum: blue text in header block

### Cell Comments

- **Anomalous historical quarters**: transcript analysis excerpt (3-4 lines max) on the revenue cell
- **Q+1 forward quarter**: beat cadence math (consensus × beat % = implied actual)
- Author: "KIN Model"
