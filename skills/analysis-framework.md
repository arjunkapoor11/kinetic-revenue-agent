# Analysis Framework

Covers `agent.py` — the quantitative analysis engine and Claude-powered research note generator.

## Changelog

### 2026-04-12 (v2 — STL decomposition)
- **Q+2+ extrapolation**: replaced manual $ QoQ decision tree with STL seasonal decomposition
- **Excess-decay dampening**: outer quarters converge toward long-run avg % QoQ (decay factor 0.85 on excess above baseline) — chosen over 4 alternatives via systematic backtest comparison
- **Hybrid framework**: revenue row = STL (best magnitude, 6.00% MAPE), guide signal = beat-cadence (best direction, 39.5% accuracy)
- **Company-specific seasonality**: last 8 quarters per season, CV-aware exponential decay weighting for high-CV companies (>0.4)
- **Forward-looking adjustments**: deal clustering -8%, NRR improving +3%, NRR declining -5%, pipeline negative -3% — overlaid on STL $ QoQ
- **Backtest engine**: 1,245 quarter predictions across 48 tickers, 16-quarter warmup
- **Consensus overrides**: `CONSENSUS_OVERRIDES` dict in ingest.py for manual corrections
- **STL trend projection**: uses `statsmodels.tsa.seasonal.STL` with period=4, robust=True
- **Columns extended**: forward quarters now project through FY ending closest to Dec 2027, all driven by STL (no consensus-only columns)
- **Pre-earnings consensus**: new `pre_earnings_consensus` DB table populated from FMP `/earnings` endpoint
- **Fiscal calendar fix**: `next_period()` derives dates from last actual period, preserving fiscal calendars; consensus matching by (year, quarter) key
- **Slack notifications**: post_to_slack tool sends guide signals to #software-dashboard

### 2026-04-12 (v1)
- Q+1 estimate: consensus × (1 + avg beat %) — anchored to sell-side
- Q+2-Q+4: $ QoQ decision tree chained off beat-adjusted Q+1
- Implied guide: Q+2 only — Q+2 estimate ÷ (1 + avg beat %)
- Excel: complete rewrite with hardcoded-vs-formula separation
- Excel: added Consensus Revenue section with full memo lines

### 2026-04-11 (initial build)
- $ QoQ decision tree framework
- Beat cadence 4Q/8Q window
- Guide inference GUIDE BELOW/IN-LINE/ABOVE signals
- Transcript anomaly detection and classification
- Cloud deployment: EC2 + RDS + Anthropic Managed Agents

---------------------------

## Overview

`agent.py` iterates over 48 SaaS tickers, pulls data from PostgreSQL, computes a suite of revenue analytics using the **Kinetic hybrid forecasting methodology** (STL decomposition + beat cadence), packages them into a structured JSON payload, sends it to Claude with a hedge-fund analyst prompt, and saves the resulting research note to the `agent_reports` table.

Entry point: `run_agent(ticker)` called from `ThreadPoolExecutor(max_workers=5)`.

## Core Methodology: Hybrid Forecast

The model uses two different approaches optimized for their respective strengths:

| Component | Method | Strength | Backtest Result |
|-----------|--------|----------|-----------------|
| **Q+1 revenue** | Beat-adjusted consensus | Best point estimate for next quarter | MAPE 7.75%, median 1.92% |
| **Q+2-Q+8 revenue** | STL seasonal decomposition | Best multi-quarter accuracy | MAPE 6.00%, median 3.47% |
| **Guide inference signal** | Beat-cadence framework | Best directional accuracy | 39.5% directional accuracy |

These can legitimately differ — the STL revenue estimate and beat-cadence guide estimate are shown side by side in the Excel output.

## Analytics Pipeline

```
get_db_data() → compute_qoq() → compute_seasonality() → flag_anomalies()
                      │
                      ├──> classify_seasonal_trend() (fallback for <12Q history)
                      ├──> compute_momentum() (last 2-3 quarters)
                      ├──> compute_qoq_yoy() (YoY change on $ QoQ)
                      │
                      ├──> stl_project() → STL trend + seasonal projection
                      ├──> extrapolate() → forward projections
                      │
               compute_beat_cadence() → from pre_earnings_consensus table
                      │
                      ├──> build_guide_inference() → implied guide signal
                      └──> compute_forward_adjustments() → deal clustering, NRR, pipeline overlays
```

### 1-7. Core Analytics (unchanged)

`get_db_data`, `compute_qoq`, `compute_seasonality`, `flag_anomalies`, `classify_seasonal_trend`, `compute_momentum`, `compute_qoq_yoy` — see previous documentation. Key change: `compute_seasonality` now uses **last 8 quarters per season** with CV-aware exponential decay weighting (decay factor 0.85 for CV > 0.4).

### 8. `stl_project(actuals, n_forward)` — STL Seasonal Decomposition

The core forecasting engine for Q+2 through Q+8. Replaces the manual $ QoQ decision tree.

**Decomposition:**
- Uses `statsmodels.tsa.seasonal.STL` with `period=4` (quarterly) and `robust=True`
- Decomposes the revenue series into trend, seasonal, and residual components
- Requires at least 12 quarters of history (3 full seasonal cycles)
- Falls back to % QoQ decision tree if fewer than 12 quarters available

**Trend projection with excess-decay dampening:**
```
For each forward quarter n:
  1. Compute raw STL projection = projected_trend + seasonal_component
  2. Compute implied % QoQ from raw projection
  3. Compute excess = implied % QoQ - long-run avg % QoQ (trailing 8Q)
  4. Dampen only the excess: dampened % QoQ = long_run_avg + excess × 0.85^(n-1)
  5. Apply dampened % QoQ to projected revenue base (compounding)
```

**Why excess-decay over alternatives (backtest comparison, 821 Q+2 predictions):**

| Approach | MAPE | Median APE | Dir Acc | Issue |
|----------|------|------------|---------|-------|
| Linear STL | 5.97% | 3.52% | 37.8% | Outer quarters too aggressive |
| Full $ decay (0.85^n) | 6.10% | 3.74% | 34.3% | Converges to zero — absurd for growing companies |
| **Excess-above-baseline decay** | **6.00%** | **3.47%** | **39.5%** | **Best balance** |
| Hard cap (120% of avg) | 7.25% | 3.92% | 33.6% | Loses seasonal structure |
| Mean reversion blend | 6.00% | 3.47% | 39.5% | Outer quarters overshoot (compounding % rate) |

Excess decay was chosen because it converges toward the long-run growth rate (not zero), preserves seasonal structure, and produces the most realistic outer-quarter projections for growing SaaS businesses.

### 9. `extrapolate()` — Forward Projection

Projects revenue for quarters through the FY ending closest to Dec 2027 (`MAX_FWD_DATE`).

**Q+1:** `consensus × (1 + avg beat %)` — beat-adjusted consensus, unchanged.

**Q+2 through Q+n:** STL decomposition with excess-decay dampening and forward-looking adjustment overlays.

**Fallback:** % QoQ decision tree (for tickers with <12 quarters of history).

**Forward-looking adjustments** (applied as multiplier on STL $ QoQ):
- Deal clustering haircut: -8% if prior quarter anomaly + transcript mentions deal/pull-forward/lumpy keywords
- NRR improving: +3% if latest transcript mentions improving net retention/expansion
- NRR declining: -5% if latest transcript mentions declining retention/churn
- Pipeline negative: -3% if latest transcript mentions pipeline weakness/elongated cycles

### 10. `compute_beat_cadence(ticker)`

Computes from FMP `/stable/earnings` endpoint (historical `revenueActual` vs `revenueEstimated`). Selects the window (4Q or 8Q) with lower standard deviation.

Also stored in `pre_earnings_consensus` DB table by `ingest.py` for backtest use.

### 11. `build_guide_inference(projections, beat_cadence)` — Hybrid Guide Signal

Uses beat-cadence framework for the guide signal (best directional accuracy):
- `beat_adjusted_actual = Q+2 consensus × (1 + beat %)`
- `implied_guide = beat_adjusted_actual / (1 + beat %)`
- Signal compares beat-adjusted actual vs consensus

The STL revenue estimate is stored separately as `projected_actual`. The Excel shows both:
- Row 9 (Total Revenue): STL estimate — best magnitude
- Row 23 (Implied Q+2 Guide): `= STL revenue / (1 + beat %)` — derives guide from our best estimate

### 12. Consensus Override Pattern

`CONSENSUS_OVERRIDES` in `ingest.py` — a dictionary of `(ticker, period) → estimated_revenue` that gets upserted after every ingest run, persisting even when FMP data is refreshed.

```python
CONSENSUS_OVERRIDES = {
    ("DDOG", "2026-01-31"): 956000000,
}
```

## Backtest Results

Tested on 867 Q+1 predictions and 821 Q+2 predictions across 47 tickers (16-quarter warmup).

### Overall

| Horizon | MAPE | Median APE | MAE | Dir Acc |
|---------|------|------------|-----|---------|
| Q+1 (beat-adjusted) | 7.75% | 1.92% | $37.6M | 40.8% |
| Q+2 (STL excess-decay) | 6.00% | 3.47% | $47.6M | 39.5% |

### By Company Size

| Segment | Q+1 MAPE | Notes |
|---------|----------|-------|
| Large cap (>$5B trailing) | 5.46% | Model works best for scaled, predictable businesses |
| Small cap | 8.85% | Higher variability, less seasonal structure |

### By Season

| Season | Q+1 MAPE | Notes |
|--------|----------|-------|
| Q1 | 5.81% | Easiest to predict |
| Q2 | 7.62% | |
| Q3 | 8.07% | |
| Q4 | 9.45% | Hardest — seasonal distortions |

## Key Design Decisions

- **STL over manual decision tree**: STL captures both trend and seasonal components statistically rather than through hand-coded classification rules. Backtest showed 6.00% MAPE vs 10.95% for the previous % QoQ approach.
- **Excess-decay, not full decay**: Full decay converges toward zero growth — unrealistic for growing SaaS. Excess-decay converges toward the long-run % QoQ baseline, which correctly assumes the business continues growing at its historical rate.
- **Hybrid guide signal**: The STL revenue estimate and beat-cadence guide can differ. This is correct — STL models the statistical trajectory while beat-cadence models management's systematic sandbagging pattern. Both are valuable signals.
- **Company-specific CV weighting**: High-CV seasons (>0.4) use exponential decay (factor 0.85) to weight recent observations more heavily. This prevents one anomalous quarter from dominating the seasonal average.
- **16-quarter warmup**: Backtest excludes the first 16 quarters per ticker. The model needs 12+ quarters for STL and early-history beat cadence is noisy (IPO-era distortions).
- **Beat cadence from pre_earnings_consensus table**: Stored separately from `consensus_estimates` (which backfills historical = actual). The `/stable/earnings` endpoint preserves the pre-report consensus.
- **Fiscal-calendar-aware period dates**: `next_period()` steps forward from the last actual date by 3 months, preserving each company's fiscal quarter-end conventions. Consensus matching uses (year, quarter) keys, not exact date strings.
- **Consensus overrides**: Manual corrections in `ingest.py` that persist across re-runs, for cases where FMP data is wrong or missing.
