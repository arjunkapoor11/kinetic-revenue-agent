"""
Kinetic Revenue Agent — Backtest Engine

Reconstructs what the model would have predicted for each historical quarter
using only data available before that quarter was reported. No lookahead bias.
No API calls — uses only data already in the DB.

Usage:
    python backtest.py                  # all tickers
    python backtest.py --ticker SNOW    # single ticker
"""

import argparse
import csv
import json
import os
import statistics
import calendar
from collections import defaultdict
from datetime import datetime

import psycopg2
from dotenv import load_dotenv

load_dotenv()

MIN_HISTORY = 16  # warmup: need 16 prior quarters before scoring (8 excluded + 8 for seasonal baselines)


# ── DB bulk reads ─────────────────────────────────────────────────────────

def load_all_data():
    """Single bulk read of all tables. Returns dicts keyed by ticker."""
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"), database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"),
        port=5432, sslmode="require")
    cur = conn.cursor()

    cur.execute("SELECT ticker, period, revenue FROM revenue_actuals ORDER BY ticker, period")
    actuals = defaultdict(list)
    for tk, per, rev in cur.fetchall():
        actuals[tk].append({"period": str(per), "revenue": rev})

    cur.execute("SELECT ticker, period, estimated_revenue FROM pre_earnings_consensus ORDER BY ticker, period")
    pec = defaultdict(dict)
    for tk, per, est in cur.fetchall():
        pec[tk][str(per)] = est

    cur.close()
    conn.close()
    return dict(actuals), dict(pec)


# ── Pure-Python extrapolation (mirrors agent.py, no API calls) ───────────

def quarter_from_date(s):
    return (datetime.strptime(s, "%Y-%m-%d").month - 1) // 3 + 1


def compute_qoq(actuals):
    out = []
    for i in range(1, len(actuals)):
        prev, cur = actuals[i - 1]["revenue"], actuals[i]["revenue"]
        q = quarter_from_date(actuals[i]["period"])
        out.append({
            "period": actuals[i]["period"],
            "quarter": f"Q{q}",
            "revenue": cur,
            "qoq_dollar_change": cur - prev,
        })
    return out


def compute_seasonality(qoq_data):
    by_q = defaultdict(list)
    for row in qoq_data:
        by_q[row["quarter"]].append(row["qoq_dollar_change"])
    seasonal = {}
    for q in ("Q1", "Q2", "Q3", "Q4"):
        all_c = by_q.get(q, [])
        if not all_c:
            continue
        c = all_c[-8:]
        n = len(c)
        avg = statistics.mean(c)
        std = statistics.stdev(c) if n > 1 else 0
        cv = abs(std / avg) if avg != 0 else 0
        if cv > 0.4 and n >= 2:
            decay = 0.85
            weights = [decay ** (n - 1 - i) for i in range(n)]
            w_avg = sum(v * w for v, w in zip(c, weights)) / sum(weights)
        else:
            w_avg = avg
        seasonal[q] = {"avg": round(w_avg), "std": round(std), "cv": cv}
    return seasonal


def classify_seasonal_trend(values, cv=0.0, pct_values=None):
    """Returns (trend, projected_value, is_pct_rate)."""
    if not values:
        return "no_data", 0, False
    if len(values) == 1:
        return "insufficient", values[0], False
    n = len(values)
    last = values[-1]
    diffs = [values[i] - values[i - 1] for i in range(1, n)]
    m = statistics.mean(values)
    sd = statistics.stdev(values)
    if m and abs(sd / m) < 0.10:
        return "flat", last, False
    up = sum(1 for d in diffs if d > 0)
    total = len(diffs)
    if up / total >= 0.6:
        if pct_values and len(pct_values) >= 2:
            avg_pct = statistics.mean(pct_values)
        else:
            return "growing", last, False
        rc = diffs[-min(3, len(diffs)):]
        if len(rc) >= 2:
            a2 = [rc[i] - rc[i - 1] for i in range(1, len(rc))]
            if all(a > 0 for a in a2):
                return "accelerating", avg_pct, True
            if all(a < 0 for a in a2):
                return "decelerating", avg_pct * 0.92, True
        return "growing", avg_pct, True
    dn = sum(1 for d in diffs if d < 0)
    if dn / total >= 0.6:
        return "declining", round(last + statistics.mean(diffs[-min(3, len(diffs)):])), False
    if cv > 0.4 and n >= 2:
        decay = 0.85
        w = [decay ** (n - 1 - i) for i in range(n)]
    else:
        w = list(range(1, n + 1))
    return "volatile", round(sum(v * wt for v, wt in zip(values, w)) / sum(w)), False


def compute_momentum(qoq_data):
    rc = [r["qoq_dollar_change"] for r in qoq_data[-3:]]
    if len(rc) < 2:
        return 1.0
    d = [rc[i] - rc[i - 1] for i in range(1, len(rc))]
    if all(x > 0 for x in d):
        return 1.03
    if all(x < 0 for x in d):
        return 0.97
    return 1.0


def compute_beat_cadence_from_history(actuals_before, pec_before):
    """Compute beat cadence from historical actuals vs pre-earnings consensus.
    No API calls — uses only DB data passed in.
    """
    beats = []
    for a in reversed(actuals_before):
        per = a["period"]
        est = pec_before.get(per)
        if est and est > 0:
            beat_pct = (a["revenue"] - est) / est * 100
            beats.append(beat_pct)
        if len(beats) >= 8:
            break

    if len(beats) < 2:
        return None

    l4 = beats[:4]
    l8 = beats[:8]
    a4 = statistics.mean(l4)
    a8 = statistics.mean(l8)
    s4 = statistics.stdev(l4) if len(l4) > 1 else float("inf")
    s8 = statistics.stdev(l8) if len(l8) > 1 else float("inf")
    sel = a4 if s4 <= s8 else a8
    return sel / 100  # as decimal


def _qoq_extrapolate(qoq, seasonal, prev_rev, q_key, momentum):
    """$ QoQ extrapolation using % QoQ for growing, $ QoQ for others."""
    by_q_dollar = defaultdict(list)
    by_q_pct = defaultdict(list)
    for row in qoq:
        by_q_dollar[row["quarter"]].append(row["qoq_dollar_change"])
        if row.get("prev_revenue") and row["prev_revenue"] > 0:
            by_q_pct[row["quarter"]].append(row["qoq_dollar_change"] / row["prev_revenue"])
        else:
            by_q_pct[row["quarter"]].append(0)

    values = (by_q_dollar.get(q_key, []))[-8:]
    pct_values = (by_q_pct.get(q_key, []))[-8:]
    cv = seasonal[q_key]["cv"] if q_key in seasonal else 0

    if values:
        _, proj, is_pct = classify_seasonal_trend(values, cv=cv, pct_values=pct_values)
        if is_pct:
            adj_qoq = round(prev_rev * proj * momentum)
        else:
            adj_qoq = round(proj * momentum)
        return prev_rev + adj_qoq
    return prev_rev


def predict_quarter(actuals_before, pec_for_quarter, pec_all_before):
    """Predict revenue for Q+1: beat-adjusted consensus."""
    if len(actuals_before) < 4:
        return None, "insufficient_data"

    qoq = compute_qoq(actuals_before)
    if len(qoq) < 2:
        return None, "insufficient_qoq"

    for i, row in enumerate(qoq):
        row["prev_revenue"] = actuals_before[i]["revenue"]

    seasonal = compute_seasonality(qoq)
    momentum = compute_momentum(qoq)
    beat_pct = compute_beat_cadence_from_history(actuals_before, pec_all_before)

    last_q = quarter_from_date(actuals_before[-1]["period"])
    next_q = ((last_q - 1 + 1) % 4) + 1
    q_key = f"Q{next_q}"
    prev_rev = actuals_before[-1]["revenue"]

    if pec_for_quarter and beat_pct is not None:
        predicted = round(pec_for_quarter * (1 + beat_pct))
        method = "beat_adjusted"
    else:
        predicted = _qoq_extrapolate(qoq, seasonal, prev_rev, q_key, momentum)
        method = "qoq_fallback"

    return predicted, method


def _stl_project_backtest(actuals_before, n_forward=2):
    """STL with excess-decay dampening — mirrors agent.py."""
    try:
        from statsmodels.tsa.seasonal import STL
        import numpy as np
    except ImportError:
        return None
    revenues = [a["revenue"] for a in actuals_before]
    if len(revenues) < 12:
        return None
    pct_8 = [(revenues[i]-revenues[i-1])/revenues[i-1]
             for i in range(max(1,len(revenues)-8), len(revenues)) if revenues[i-1]>0]
    pct_4 = pct_8[-4:] if len(pct_8)>=4 else pct_8
    if pct_4 and pct_8 and (np.mean(pct_4)-np.mean(pct_8))>0.01 and len(revenues)>12:
        revenues = revenues[-12:]
    series = np.array(revenues, dtype=float)
    stl = STL(series, period=4, robust=True)
    result = stl.fit()
    trend = result.trend
    seasonal_comp = result.seasonal
    trend_diffs = [trend[i] - trend[i - 1] for i in range(max(1, len(trend) - 4), len(trend))]
    avg_inc = float(np.mean(trend_diffs))
    last_trend = float(trend[-1])
    last_seasonal = {}
    for i in range(len(seasonal_comp) - 1, max(len(seasonal_comp) - 5, -1), -1):
        pos = i % 4
        if pos not in last_seasonal:
            last_seasonal[pos] = float(seasonal_comp[i])
    pct_8q = []
    for i in range(max(1, len(revenues) - 8), len(revenues)):
        if revenues[i - 1] > 0:
            pct_8q.append((revenues[i] - revenues[i - 1]) / revenues[i - 1])
    pct_4q = pct_8q[-4:] if len(pct_8q) >= 4 else pct_8q
    avg_8q = float(np.mean(pct_8q)) if pct_8q else 0
    avg_4q = float(np.mean(pct_4q)) if pct_4q else 0
    long_run_pct = avg_4q if (avg_4q - avg_8q) > 0.01 else avg_8q
    EXCESS_DECAY = 0.85
    forward = {}
    last_pos = (len(revenues) - 1) % 4
    prev_rev = float(revenues[-1])
    for i in range(1, n_forward + 1):
        raw_trend = last_trend + avg_inc * i
        pos = (last_pos + i) % 4
        raw_rev = raw_trend + last_seasonal.get(pos, 0)
        stl_pct = (raw_rev - prev_rev) / prev_rev if prev_rev > 0 else 0
        excess = stl_pct - long_run_pct
        dampened_pct = long_run_pct + excess * (EXCESS_DECAY ** (i - 1))
        proj_rev = prev_rev * (1 + dampened_pct)
        forward[i] = round(proj_rev)
        prev_rev = proj_rev
    return forward


def predict_q2(actuals_before, pec_for_q1, pec_all_before, pec_for_q2=None):
    """Predict Q+2: beat-adjusted Q+1, then STL for Q+2 (fallback: % QoQ)."""
    if len(actuals_before) < 4:
        return None, None, "insufficient_data"

    # Step 1: predict Q+1 via beat-adjusted consensus
    q1_predicted, q1_method = predict_quarter(actuals_before, pec_for_q1, pec_all_before)
    if q1_predicted is None:
        return None, None, "q1_failed"

    # Step 2: predict Q+2 via STL if enough data
    stl_fwd = _stl_project_backtest(actuals_before, n_forward=2)
    if stl_fwd and 2 in stl_fwd:
        q2_predicted = stl_fwd[2]
        return q2_predicted, q1_predicted, "stl_decomposition"

    # Fallback: % QoQ chain
    qoq = compute_qoq(actuals_before)
    if len(qoq) < 2:
        return q1_predicted, q1_predicted, "q2_no_qoq"

    for i, row in enumerate(qoq):
        row["prev_revenue"] = actuals_before[i]["revenue"]

    seasonal = compute_seasonality(qoq)
    momentum = compute_momentum(qoq)

    last_q = quarter_from_date(actuals_before[-1]["period"])
    q2_num = ((last_q - 1 + 2) % 4) + 1
    q2_key = f"Q{q2_num}"

    q2_predicted = _qoq_extrapolate(qoq, seasonal, q1_predicted, q2_key, momentum)

    return q2_predicted, q1_predicted, "qoq_chain"


# ── Backtest runners ─────────────────────────────────────────────────────

def run_backtest(all_actuals, all_pec, ticker_filter=None):
    """Q+1 backtest: predict each quarter using data through the prior quarter."""
    results = []

    tickers = [ticker_filter] if ticker_filter else sorted(all_actuals.keys())

    for tk in tickers:
        actuals = all_actuals.get(tk, [])
        pec = all_pec.get(tk, {})

        if not actuals or len(actuals) < MIN_HISTORY:
            continue

        pec_periods = set(pec.keys())

        for idx in range(MIN_HISTORY, len(actuals)):
            target = actuals[idx]
            target_period = target["period"]

            if target_period not in pec_periods:
                continue

            actuals_before = actuals[:idx]
            pec_for_q = pec[target_period]
            pec_before = {p: v for p, v in pec.items() if p < target_period}

            predicted, method = predict_quarter(actuals_before, pec_for_q, pec_before)

            if predicted is None:
                continue

            actual_rev = target["revenue"]
            error = predicted - actual_rev
            abs_error = abs(error)
            pct_error = abs_error / actual_rev * 100 if actual_rev else 0
            direction_correct = (predicted >= actual_rev) == (pec_for_q <= actual_rev)

            q_label = f"Q{quarter_from_date(target_period)}"

            results.append({
                "ticker": tk,
                "period": target_period,
                "quarter": q_label,
                "actual": actual_rev,
                "predicted": predicted,
                "consensus": pec_for_q,
                "error": error,
                "abs_error": abs_error,
                "pct_error": round(pct_error, 2),
                "method": method,
                "direction_correct": direction_correct,
                "bias": "over" if error > 0 else "under",
                "revenue_scale": actual_rev,
                "horizon": "Q+1",
            })

    return results


def run_backtest_q2(all_actuals, all_pec, ticker_filter=None):
    """Q+2 backtest: predict each quarter using data through two quarters prior.

    For target quarter Q at index idx:
      - actuals_before = actuals[:idx-1]  (through Q-2, excluding Q-1)
      - pec_for_q1 = pre-earnings consensus for Q-1 (actuals[idx-1])
      - Predict Q-1 via beat-adjusted, then chain $ QoQ to predict Q
    """
    results = []

    tickers = [ticker_filter] if ticker_filter else sorted(all_actuals.keys())

    for tk in tickers:
        actuals = all_actuals.get(tk, [])
        pec = all_pec.get(tk, {})

        if not actuals or len(actuals) < MIN_HISTORY + 1:
            continue

        pec_periods = set(pec.keys())

        for idx in range(MIN_HISTORY + 1, len(actuals)):
            target = actuals[idx]
            target_period = target["period"]

            # Need pre-earnings consensus for Q-1 (to do beat-adjusted Q+1)
            q_minus_1 = actuals[idx - 1]
            q1_period = q_minus_1["period"]
            if q1_period not in pec_periods:
                continue

            # Data available: actuals through Q-2 only
            actuals_before = actuals[:idx - 1]
            pec_for_q1 = pec[q1_period]
            pec_for_q2 = pec.get(target_period)  # consensus for Q+2 if available
            pec_before = {p: v for p, v in pec.items() if p < q1_period}

            q2_pred, q1_pred, method = predict_q2(actuals_before, pec_for_q1, pec_before, pec_for_q2)

            if q2_pred is None:
                continue

            actual_rev = target["revenue"]
            error = q2_pred - actual_rev
            abs_error = abs(error)
            pct_error = abs_error / actual_rev * 100 if actual_rev else 0

            # Directional: did we predict over/under vs what actually happened?
            # Use Q-1's consensus as reference if available for target quarter
            target_con = pec.get(target_period)
            if target_con:
                direction_correct = (q2_pred >= actual_rev) == (target_con <= actual_rev)
            else:
                direction_correct = False

            q_label = f"Q{quarter_from_date(target_period)}"

            results.append({
                "ticker": tk,
                "period": target_period,
                "quarter": q_label,
                "actual": actual_rev,
                "predicted": q2_pred,
                "q1_predicted": q1_pred,
                "consensus": target_con,
                "error": error,
                "abs_error": abs_error,
                "pct_error": round(pct_error, 2),
                "method": method,
                "direction_correct": direction_correct,
                "bias": "over" if error > 0 else "under",
                "revenue_scale": actual_rev,
                "horizon": "Q+2",
            })

    return results


# ── Metrics computation ──────────────────────────────────────────────────

def compute_metrics(results):
    if not results:
        return {}

    abs_errors = [r["abs_error"] for r in results]
    pct_errors = [r["pct_error"] for r in results]
    directions = [r["direction_correct"] for r in results]
    biases = [r["bias"] for r in results]

    return {
        "n_quarters": len(results),
        "mae_dollars": round(statistics.mean(abs_errors)),
        "mae_millions": round(statistics.mean(abs_errors) / 1e6, 1),
        "mape": round(statistics.mean(pct_errors), 2),
        "median_ape": round(statistics.median(pct_errors), 2),
        "directional_accuracy": round(sum(directions) / len(directions) * 100, 1),
        "over_pct": round(sum(1 for b in biases if b == "over") / len(biases) * 100, 1),
        "under_pct": round(sum(1 for b in biases if b == "under") / len(biases) * 100, 1),
    }


def metrics_by_ticker(results):
    by_tk = defaultdict(list)
    for r in results:
        by_tk[r["ticker"]].append(r)
    out = {}
    for tk, rows in sorted(by_tk.items()):
        m = compute_metrics(rows)
        over = sum(1 for r in rows if r["bias"] == "over")
        m["bias_direction"] = "over" if over > len(rows) / 2 else "under"
        m["ticker"] = tk
        out[tk] = m
    return out


def metrics_by_season(results):
    by_q = defaultdict(list)
    for r in results:
        by_q[r["quarter"]].append(r)
    out = {}
    for q in ("Q1", "Q2", "Q3", "Q4"):
        rows = by_q.get(q, [])
        if rows:
            out[q] = compute_metrics(rows)
    return out


def metrics_by_size(results, all_actuals):
    """Split by company size: last 4Q revenue > $5B = large cap."""
    large = []
    small = []
    for r in results:
        tk = r["ticker"]
        acts = all_actuals.get(tk, [])
        trailing = sum(a["revenue"] for a in acts[-4:]) if len(acts) >= 4 else 0
        if trailing > 5_000_000_000:
            large.append(r)
        else:
            small.append(r)
    return {
        "large_cap": compute_metrics(large),
        "small_cap": compute_metrics(small),
    }


# ── Output ────────────────────────────────────────────────────────────────

def print_results(results, all_actuals):
    overall = compute_metrics(results)
    by_tk = metrics_by_ticker(results)
    by_season = metrics_by_season(results)
    by_size = metrics_by_size(results, all_actuals)

    print()
    print("=" * 70)
    print("  KINETIC REVENUE MODEL — BACKTEST RESULTS")
    print("=" * 70)

    print(f"\n  Quarters tested: {overall['n_quarters']}")
    print(f"  Tickers: {len(by_tk)}")
    print(f"\n  Overall Accuracy:")
    print(f"    MAE:                  ${overall['mae_millions']}M")
    print(f"    MAPE:                 {overall['mape']}%")
    print(f"    Median APE:           {overall['median_ape']}%")
    print(f"    Directional Accuracy: {overall['directional_accuracy']}%")
    print(f"    Bias:                 {overall['over_pct']}% over / {overall['under_pct']}% under")

    print(f"\n  By Company Size:")
    for label, m in by_size.items():
        if m:
            print(f"    {label:12s}  MAE=${m['mae_millions']}M  MAPE={m['mape']}%  Dir={m['directional_accuracy']}%  n={m['n_quarters']}")

    print(f"\n  By Season:")
    for q in ("Q1", "Q2", "Q3", "Q4"):
        m = by_season.get(q)
        if m:
            print(f"    {q}:  MAE=${m['mae_millions']}M  MAPE={m['mape']}%  Dir={m['directional_accuracy']}%  n={m['n_quarters']}")

    # Sort by MAPE
    sorted_tickers = sorted(by_tk.values(), key=lambda x: x["mape"])

    print(f"\n  5 BEST Tickers (lowest MAPE):")
    for m in sorted_tickers[:5]:
        print(f"    {m['ticker']:6s}  MAPE={m['mape']:5.1f}%  MAE=${m['mae_millions']:>6.1f}M  Dir={m['directional_accuracy']:5.1f}%  Bias={m['bias_direction']:5s}  n={m['n_quarters']}")

    print(f"\n  5 WORST Tickers (highest MAPE):")
    for m in sorted_tickers[-5:]:
        print(f"    {m['ticker']:6s}  MAPE={m['mape']:5.1f}%  MAE=${m['mae_millions']:>6.1f}M  Dir={m['directional_accuracy']:5.1f}%  Bias={m['bias_direction']:5s}  n={m['n_quarters']}")

    print(f"\n  Full Per-Ticker Results (sorted by MAPE, worst to best):")
    for m in reversed(sorted_tickers):
        print(f"    {m['ticker']:6s}  MAPE={m['mape']:5.1f}%  MAE=${m['mae_millions']:>7.1f}M  Dir={m['directional_accuracy']:5.1f}%  Bias={m['bias_direction']:5s}  n={m['n_quarters']}")

    return overall, by_tk, by_season, by_size


def save_csv(results, path="backtest_results.csv"):
    if not results:
        return
    fields = ["ticker", "period", "quarter", "horizon", "actual", "predicted", "consensus",
              "error", "abs_error", "pct_error", "method", "direction_correct", "bias"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in sorted(results, key=lambda x: (x["ticker"], x["period"])):
            w.writerow(r)
    print(f"\n  Saved {len(results)} rows to {path}")


def save_summary(overall, by_tk, by_season, by_size, path="backtest_summary.json"):
    summary = {
        "generated": datetime.now().isoformat(),
        "overall": overall,
        "by_season": by_season,
        "by_size": by_size,
        "by_ticker": {tk: m for tk, m in by_tk.items()},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"  Saved summary to {path}")


# ── Main ──────────────────────────────────────────────────────────────────

def print_comparison(q1_results, q2_results, all_actuals):
    """Print Q+1 vs Q+2 accuracy side by side."""
    q1m = compute_metrics(q1_results)
    q2m = compute_metrics(q2_results)

    print()
    print("=" * 70)
    print("  Q+1 vs Q+2 ACCURACY COMPARISON")
    print("=" * 70)

    print(f"\n  {'Metric':<26s}  {'Q+1':>10s}  {'Q+2':>10s}  {'Delta':>10s}")
    print(f"  {'-'*26}  {'-'*10}  {'-'*10}  {'-'*10}")
    for label, k, fmt in [
        ("Quarters tested", "n_quarters", "d"),
        ("MAE ($M)", "mae_millions", ".1f"),
        ("MAPE (%)", "mape", ".2f"),
        ("Median APE (%)", "median_ape", ".2f"),
        ("Directional Acc (%)", "directional_accuracy", ".1f"),
    ]:
        v1 = q1m.get(k, 0)
        v2 = q2m.get(k, 0)
        delta = v2 - v1 if isinstance(v1, (int, float)) else 0
        d_str = f"+{delta:{fmt}}" if delta >= 0 else f"{delta:{fmt}}"
        print(f"  {label:<26s}  {v1:>10{fmt}}  {v2:>10{fmt}}  {d_str:>10s}")

    # By season comparison
    q1_season = metrics_by_season(q1_results)
    q2_season = metrics_by_season(q2_results)
    print(f"\n  By Season (MAPE):")
    print(f"  {'Season':<8s}  {'Q+1':>8s}  {'Q+2':>8s}  {'Delta':>8s}")
    print(f"  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")
    for q in ("Q1", "Q2", "Q3", "Q4"):
        m1 = q1_season.get(q, {})
        m2 = q2_season.get(q, {})
        v1 = m1.get("mape", 0)
        v2 = m2.get("mape", 0)
        d = v2 - v1
        print(f"  {q:<8s}  {v1:>7.1f}%  {v2:>7.1f}%  {d:>+7.1f}%")

    # Per-ticker Q+1 vs Q+2
    q1_tk = metrics_by_ticker(q1_results)
    q2_tk = metrics_by_ticker(q2_results)
    all_tks = sorted(set(q1_tk.keys()) | set(q2_tk.keys()))

    # Best 5 for Q+2
    q2_sorted = sorted(
        [(tk, q2_tk[tk]) for tk in all_tks if tk in q2_tk],
        key=lambda x: x[1]["mape"])

    print(f"\n  5 BEST Tickers for Q+2 (lowest MAPE):")
    for tk, m2 in q2_sorted[:5]:
        m1 = q1_tk.get(tk, {})
        v1 = m1.get("mape", 0)
        print(f"    {tk:6s}  Q+1={v1:5.1f}%  Q+2={m2['mape']:5.1f}%  MAE=${m2['mae_millions']:>6.1f}M  n={m2['n_quarters']}")

    print(f"\n  5 WORST Tickers for Q+2 (highest MAPE):")
    for tk, m2 in q2_sorted[-5:]:
        m1 = q1_tk.get(tk, {})
        v1 = m1.get("mape", 0)
        print(f"    {tk:6s}  Q+1={v1:5.1f}%  Q+2={m2['mape']:5.1f}%  MAE=${m2['mae_millions']:>6.1f}M  n={m2['n_quarters']}")


def main():
    parser = argparse.ArgumentParser(description="Kinetic Revenue Model Backtest")
    parser.add_argument("--ticker", type=str, default=None, help="Single ticker to test")
    args = parser.parse_args()

    print("Loading data from DB...")
    all_actuals, all_pec = load_all_data()
    print(f"  {len(all_actuals)} tickers with actuals, {len(all_pec)} with pre-earnings consensus")

    # Q+1 backtest
    print("Running Q+1 backtest...")
    q1_results = run_backtest(all_actuals, all_pec, ticker_filter=args.ticker)
    print(f"  {len(q1_results)} Q+1 predictions")

    # Q+2 backtest
    print("Running Q+2 backtest...")
    q2_results = run_backtest_q2(all_actuals, all_pec, ticker_filter=args.ticker)
    print(f"  {len(q2_results)} Q+2 predictions")

    if not q1_results and not q2_results:
        print("  No results — check data availability.")
        return

    # Print Q+1 results
    if q1_results:
        print_results(q1_results, all_actuals)

    # Print comparison
    if q1_results and q2_results:
        print_comparison(q1_results, q2_results, all_actuals)

    # Save combined CSV
    all_results = q1_results + q2_results
    save_csv(all_results)
    q1m = compute_metrics(q1_results) if q1_results else {}
    q2m = compute_metrics(q2_results) if q2_results else {}
    q1_tk = metrics_by_ticker(q1_results) if q1_results else {}
    q2_tk = metrics_by_ticker(q2_results) if q2_results else {}
    summary = {
        "generated": datetime.now().isoformat(),
        "q1_overall": q1m,
        "q2_overall": q2m,
        "q1_by_season": metrics_by_season(q1_results) if q1_results else {},
        "q2_by_season": metrics_by_season(q2_results) if q2_results else {},
        "q1_by_ticker": q1_tk,
        "q2_by_ticker": q2_tk,
    }
    with open("backtest_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"  Saved summary to backtest_summary.json")


if __name__ == "__main__":
    main()
