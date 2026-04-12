"""
Systematic comparison of 5 STL dampening approaches.
Runs backtest Q+2 for each and samples DDOG/MDB forward quarters.
"""

import os
import sys
import statistics
import math
from collections import defaultdict
from datetime import datetime

import numpy as np
import psycopg2
from dotenv import load_dotenv
from statsmodels.tsa.seasonal import STL

load_dotenv()

MIN_HISTORY = 16


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"), database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"),
        port=5432, sslmode="require")


def load_all():
    conn = get_conn()
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


def quarter_from_date(s):
    return (datetime.strptime(s, "%Y-%m-%d").month - 1) // 3 + 1


def compute_beat(actuals_before, pec_before):
    beats = []
    for a in reversed(actuals_before):
        est = pec_before.get(a["period"])
        if est and est > 0:
            beats.append((a["revenue"] - est) / est)
        if len(beats) >= 8:
            break
    if len(beats) < 2:
        return None
    l4 = beats[:4]
    l8 = beats[:8]
    s4 = statistics.stdev(l4) if len(l4) > 1 else float("inf")
    s8 = statistics.stdev(l8) if len(l8) > 1 else float("inf")
    return statistics.mean(l4) if s4 <= s8 else statistics.mean(l8)


# ── 5 STL approaches ─────────────────────────────────────────────────────

def stl_approach(actuals, n_forward, mode="linear"):
    """Run STL and project forward using the specified approach."""
    revenues = [a["revenue"] for a in actuals]
    if len(revenues) < 12:
        return None

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
    last_pos = (len(revenues) - 1) % 4

    # Trailing 8Q stats
    pct_qoqs = []
    dollar_qoqs = []
    for i in range(max(1, len(revenues) - 8), len(revenues)):
        if revenues[i - 1] > 0:
            pct_qoqs.append((revenues[i] - revenues[i - 1]) / revenues[i - 1])
            dollar_qoqs.append(revenues[i] - revenues[i - 1])
    long_run_pct = float(np.mean(pct_qoqs)) if pct_qoqs else 0
    avg_dollar_qoq = float(np.mean(dollar_qoqs)) if dollar_qoqs else 0

    forward = {}
    prev_rev = float(revenues[-1])

    if mode == "linear":
        # Approach 1: pure linear STL
        for i in range(1, n_forward + 1):
            raw_trend = last_trend + avg_inc * i
            pos = (last_pos + i) % 4
            proj_rev = raw_trend + last_seasonal.get(pos, 0)
            forward[i] = round(proj_rev)
            prev_rev = proj_rev

    elif mode == "full_decay":
        # Approach 2: decay on full trend increment
        cum_trend = last_trend
        for i in range(1, n_forward + 1):
            cum_trend += avg_inc * (0.85 ** (i - 1))
            pos = (last_pos + i) % 4
            forward[i] = round(cum_trend + last_seasonal.get(pos, 0))
            prev_rev = forward[i]

    elif mode == "excess_decay":
        # Approach 3: decay on excess above long-run avg % QoQ
        for i in range(1, n_forward + 1):
            raw_trend = last_trend + avg_inc * i
            pos = (last_pos + i) % 4
            raw_rev = raw_trend + last_seasonal.get(pos, 0)
            stl_pct = (raw_rev - prev_rev) / prev_rev if prev_rev > 0 else 0
            excess = stl_pct - long_run_pct
            dampened_pct = long_run_pct + excess * (0.85 ** (i - 1))
            proj_rev = prev_rev * (1 + dampened_pct)
            forward[i] = round(proj_rev)
            prev_rev = proj_rev

    elif mode == "hard_cap":
        # Approach 4: STL $ QoQ capped at 120% of trailing 8Q avg
        cap = abs(avg_dollar_qoq) * 1.2
        for i in range(1, n_forward + 1):
            raw_trend = last_trend + avg_inc * i
            pos = (last_pos + i) % 4
            raw_rev = raw_trend + last_seasonal.get(pos, 0)
            raw_qoq = raw_rev - prev_rev
            if abs(raw_qoq) > cap:
                raw_qoq = cap if raw_qoq > 0 else -cap
            proj_rev = prev_rev + raw_qoq
            forward[i] = round(proj_rev)
            prev_rev = proj_rev

    elif mode == "mean_revert":
        # Approach 5: blend STL with trailing avg % QoQ, STL weight fades
        for i in range(1, n_forward + 1):
            raw_trend = last_trend + avg_inc * i
            pos = (last_pos + i) % 4
            raw_rev = raw_trend + last_seasonal.get(pos, 0)
            stl_pct = (raw_rev - prev_rev) / prev_rev if prev_rev > 0 else 0
            # STL weight: 100% at Q+1, linearly to 0% at Q+8
            stl_weight = max(0, 1.0 - (i - 1) / 7.0)
            blended_pct = stl_weight * stl_pct + (1 - stl_weight) * long_run_pct
            proj_rev = prev_rev * (1 + blended_pct)
            forward[i] = round(proj_rev)
            prev_rev = proj_rev

    return forward


# ── Q+2 backtest for a given mode ────────────────────────────────────────

def backtest_q2(all_actuals, all_pec, mode):
    results = []
    for tk in sorted(all_actuals.keys()):
        actuals = all_actuals[tk]
        pec = all_pec.get(tk, {})
        if len(actuals) < MIN_HISTORY + 1:
            continue
        pec_periods = set(pec.keys())

        for idx in range(MIN_HISTORY + 1, len(actuals)):
            target = actuals[idx]
            target_period = target["period"]
            q_minus_1 = actuals[idx - 1]
            q1_period = q_minus_1["period"]
            if q1_period not in pec_periods:
                continue

            actuals_before = actuals[:idx - 1]
            pec_for_q1 = pec[q1_period]
            pec_before = {p: v for p, v in pec.items() if p < q1_period}

            # Q+1: beat-adjusted
            beat = compute_beat(actuals_before, pec_before)
            if beat is not None and pec_for_q1:
                q1_pred = round(pec_for_q1 * (1 + beat))
            else:
                continue

            # Q+2: STL with given mode
            stl_fwd = stl_approach(actuals_before, n_forward=2, mode=mode)
            if stl_fwd and 2 in stl_fwd:
                q2_pred = stl_fwd[2]
            else:
                continue

            actual_rev = target["revenue"]
            pct_error = abs(q2_pred - actual_rev) / actual_rev * 100 if actual_rev else 0
            target_con = pec.get(target_period)
            if target_con:
                direction_correct = (q2_pred >= actual_rev) == (target_con <= actual_rev)
            else:
                direction_correct = False

            results.append({
                "ticker": tk,
                "pct_error": pct_error,
                "direction_correct": direction_correct,
            })

    mape = statistics.mean([r["pct_error"] for r in results])
    median = statistics.median([r["pct_error"] for r in results])
    dir_acc = sum(r["direction_correct"] for r in results) / len(results) * 100
    return len(results), round(mape, 2), round(median, 2), round(dir_acc, 1)


# ── Sample forward quarters for DDOG and MDB ─────────────────────────────

def sample_ticker(all_actuals, tk, mode):
    actuals = all_actuals[tk]
    stl_fwd = stl_approach(actuals, n_forward=8, mode=mode)
    if not stl_fwd:
        return []
    prev = actuals[-1]["revenue"]
    out = []
    for i in range(1, 9):
        rev = stl_fwd[i]
        qoq = rev - prev
        out.append(qoq)
        prev = rev
    return out


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    print("Loading data...")
    all_actuals, all_pec = load_all()

    modes = [
        ("linear", "1. Linear STL (baseline)"),
        ("full_decay", "2. Full $ QoQ decay (0.85^n)"),
        ("excess_decay", "3. Excess-above-baseline decay"),
        ("hard_cap", "4. Hard cap (120% of 8Q avg)"),
        ("mean_revert", "5. Mean reversion blend"),
    ]

    print("\n" + "=" * 80)
    print("  STL DAMPENING APPROACH COMPARISON")
    print("=" * 80)

    # Backtest results
    print(f"\n{'Approach':<35s}  {'N':>5s}  {'MAPE':>7s}  {'MedAPE':>7s}  {'DirAcc':>7s}")
    print("-" * 70)
    bt_results = {}
    for mode, label in modes:
        n, mape, median, dir_acc = backtest_q2(all_actuals, all_pec, mode)
        bt_results[mode] = (mape, dir_acc)
        print(f"  {label:<33s}  {n:>5d}  {mape:>6.2f}%  {median:>6.2f}%  {dir_acc:>6.1f}%")

    # DDOG sample
    for tk in ["DDOG", "MDB"]:
        print(f"\n  {tk} $ QoQ (Q+2 through Q+8):")
        print(f"  {'Approach':<35s}  {'Q+2':>7s}  {'Q+3':>7s}  {'Q+4':>7s}  {'Q+5':>7s}  {'Q+6':>7s}  {'Q+7':>7s}  {'Q+8':>7s}")
        print("  " + "-" * 84)
        for mode, label in modes:
            qoqs = sample_ticker(all_actuals, tk, mode)
            if len(qoqs) >= 8:
                vals = "  ".join(f"{q/1e6:>+6.1f}M" for q in qoqs[1:])  # Q+2 through Q+8
                print(f"  {label:<35s}  {vals}")

        # Historical context
        actuals = all_actuals[tk]
        print(f"  {'Historical (last 4Q)':<35s}  ", end="")
        for a_idx in range(len(actuals) - 4, len(actuals)):
            qoq = actuals[a_idx]["revenue"] - actuals[a_idx - 1]["revenue"]
            print(f"{qoq/1e6:>+6.1f}M  ", end="")
        print()

    print(f"\n{'=' * 80}")
    print("  RECOMMENDATION")
    print("=" * 80)
    best = min(bt_results.items(), key=lambda x: x[1][0])
    print(f"\n  Lowest MAPE: {best[0]} ({best[1][0]}%)")
    best_dir = max(bt_results.items(), key=lambda x: x[1][1])
    print(f"  Best directional: {best_dir[0]} ({best_dir[1][1]}%)")


if __name__ == "__main__":
    main()
