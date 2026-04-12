"""
Kinetic Revenue Agent — Earnings Prep Data Fetcher

Fetches and returns structured data for earnings preparation.
No document generation — returns raw JSON for the Claude agent to synthesize.

Usage:
    python earnings_prep.py SNOW
"""

import json
import os
import sys
import statistics
import calendar
from collections import defaultdict
from datetime import datetime

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def get_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"), database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"),
        port=5432, sslmode="require")


def quarter_from_date(s):
    return (datetime.strptime(s, "%Y-%m-%d").month - 1) // 3 + 1


def next_period(period_str, steps=1):
    d = datetime.strptime(period_str, "%Y-%m-%d")
    for _ in range(steps):
        m = d.month + 3
        y = d.year
        if m > 12:
            m -= 12
            y += 1
        d = datetime(y, m, calendar.monthrange(y, m)[1])
    return d.strftime("%Y-%m-%d")


def fetch_earnings_data(ticker):
    """Fetch all data needed for earnings prep. Returns a dict (JSON-serializable)."""
    conn = get_conn()
    cur = conn.cursor()

    # ── Revenue actuals ───────────────────────────────────────────────────
    cur.execute("SELECT period, revenue FROM revenue_actuals WHERE ticker=%s ORDER BY period", (ticker,))
    actuals = [(str(r[0]), r[1]) for r in cur.fetchall()]

    if len(actuals) < 4:
        cur.close()
        conn.close()
        return {"error": f"Insufficient data for {ticker}: {len(actuals)} quarters"}

    # ── Consensus estimates ───────────────────────────────────────────────
    cur.execute("SELECT period, estimated_revenue FROM consensus_estimates WHERE ticker=%s ORDER BY period", (ticker,))
    estimates = {str(r[0]): r[1] for r in cur.fetchall()}

    # ── Pre-earnings consensus ────────────────────────────────────────────
    cur.execute("SELECT period, estimated_revenue FROM pre_earnings_consensus WHERE ticker=%s ORDER BY period", (ticker,))
    pec = {str(r[0]): r[1] for r in cur.fetchall()}

    # ── Transcript analyses ───────────────────────────────────────────────
    cur.execute("""SELECT period, transcript_analysis FROM transcripts
                   WHERE ticker=%s AND transcript_analysis IS NOT NULL
                   ORDER BY period""", (ticker,))
    transcript_analyses = {str(r[0]): r[1] for r in cur.fetchall()}

    # ── Most recent agent report ──────────────────────────────────────────
    cur.execute("""SELECT report, created_at FROM agent_reports
                   WHERE ticker=%s ORDER BY created_at DESC LIMIT 1""", (ticker,))
    latest_report_row = cur.fetchone()
    latest_report = latest_report_row[0] if latest_report_row else None

    cur.close()
    conn.close()

    # ── Derived analytics ─────────────────────────────────────────────────

    last_period, last_rev = actuals[-1]
    q1_period = next_period(last_period, 1)
    q2_period = next_period(last_period, 2)

    # Beat cadence from pre-earnings consensus
    beat_history = []
    for per, rev in reversed(actuals):
        est = pec.get(per)
        if est and est > 0:
            beat_history.append({
                "period": per,
                "actual": rev,
                "consensus": est,
                "beat_pct": round((rev - est) / est * 100, 2),
            })
        if len(beat_history) >= 8:
            break

    beat_4q_avg = round(statistics.mean([b["beat_pct"] for b in beat_history[:4]]), 2) if len(beat_history) >= 4 else None
    beat_8q_avg = round(statistics.mean([b["beat_pct"] for b in beat_history[:8]]), 2) if len(beat_history) >= 2 else None
    std_4q = statistics.stdev([b["beat_pct"] for b in beat_history[:4]]) if len(beat_history) >= 4 else float("inf")
    std_8q = statistics.stdev([b["beat_pct"] for b in beat_history[:8]]) if len(beat_history) >= 8 else float("inf")
    selected_beat = beat_4q_avg if std_4q <= std_8q else beat_8q_avg
    selected_window = "4Q" if std_4q <= std_8q else "8Q"

    # Q+1 and Q+2 consensus lookup by (year, quarter)
    actual_yqs = set()
    for per, _ in actuals:
        d = datetime.strptime(per, "%Y-%m-%d")
        actual_yqs.add((d.year, quarter_from_date(per)))

    def find_consensus(period):
        yq = (datetime.strptime(period, "%Y-%m-%d").year, quarter_from_date(period))
        for per, est in estimates.items():
            d = datetime.strptime(per, "%Y-%m-%d")
            if (d.year, quarter_from_date(per)) == yq and yq not in actual_yqs:
                return est
        return None

    q1_consensus = find_consensus(q1_period)
    q2_consensus = find_consensus(q2_period)

    # Beat-adjusted estimate
    beat_adjusted_q1 = round(q1_consensus * (1 + selected_beat / 100)) if q1_consensus and selected_beat else None

    # STL projection
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    stl_q1, stl_q2 = None, None
    try:
        from agent import stl_project
        acts_dicts = [{"period": p, "revenue": r} for p, r in actuals]
        stl_result = stl_project(acts_dicts, 2)
        if stl_result:
            stl_q1 = stl_result[0].get(1)
            stl_q2 = stl_result[0].get(2)
    except Exception:
        pass

    # Implied guide
    implied_guide = round(stl_q2 / (1 + selected_beat / 100)) if stl_q2 and selected_beat else None

    # Year-ago quarter revenue
    ya_rev = None
    for per, rev in actuals:
        d = datetime.strptime(per, "%Y-%m-%d")
        q1_d = datetime.strptime(q1_period, "%Y-%m-%d")
        if d.year == q1_d.year - 1 and quarter_from_date(per) == quarter_from_date(q1_period):
            ya_rev = rev
            break

    # QoQ history (last 8)
    qoq_history = []
    for i in range(max(1, len(actuals) - 8), len(actuals)):
        prev_rev = actuals[i - 1][1]
        cur_per, cur_rev = actuals[i]
        qoq_history.append({
            "period": cur_per,
            "quarter": f"Q{quarter_from_date(cur_per)}",
            "revenue": cur_rev,
            "qoq_dollar": cur_rev - prev_rev,
            "qoq_pct": round((cur_rev - prev_rev) / prev_rev * 100, 2) if prev_rev else 0,
        })

    # Anomalies (last 8 quarters)
    by_q = defaultdict(list)
    for i in range(1, len(actuals)):
        prev_rev = actuals[i - 1][1]
        cur_per, cur_rev = actuals[i]
        q = quarter_from_date(cur_per)
        by_q[f"Q{q}"].append({"period": cur_per, "change": cur_rev - prev_rev})

    anomalies = []
    for q, rows in by_q.items():
        changes = [r["change"] for r in rows]
        if len(changes) < 3:
            continue
        avg = statistics.mean(changes)
        std = statistics.stdev(changes)
        if std == 0:
            continue
        for r in rows[-4:]:
            dev = abs(r["change"] - avg) / std
            if dev > 1.5:
                ta = transcript_analyses.get(r["period"])
                anomalies.append({
                    "period": r["period"],
                    "quarter": q,
                    "qoq_dollar": r["change"],
                    "deviation_sigma": round(dev, 2),
                    "transcript_analysis": ta[:1000] if ta else None,
                })

    # Most recent transcript analysis
    latest_ta_period = max(transcript_analyses.keys()) if transcript_analyses else None
    latest_ta = transcript_analyses.get(latest_ta_period) if latest_ta_period else None

    # Momentum
    recent_qoq = [r["qoq_dollar"] for r in qoq_history[-3:]]
    if len(recent_qoq) >= 2:
        diffs = [recent_qoq[i] - recent_qoq[i - 1] for i in range(1, len(recent_qoq))]
        if all(d > 0 for d in diffs):
            momentum = "ACCELERATING"
        elif all(d < 0 for d in diffs):
            momentum = "DECELERATING"
        else:
            momentum = "STABLE"
    else:
        momentum = "NEUTRAL"

    # ── Assemble output ───────────────────────────────────────────────────

    return {
        "ticker": ticker,
        "generated": datetime.now().isoformat(),
        "next_quarter": {
            "period": q1_period,
            "calendar_quarter": f"Q{quarter_from_date(q1_period)}",
        },
        "revenue_setup": {
            "last_actual": {"period": last_period, "revenue": last_rev},
            "q1_consensus": q1_consensus,
            "q1_beat_adjusted": beat_adjusted_q1,
            "q1_stl_estimate": stl_q1,
            "q2_consensus": q2_consensus,
            "q2_stl_estimate": stl_q2,
            "q2_implied_guide": implied_guide,
            "q2_guide_gap_pct": round((implied_guide - q2_consensus) / q2_consensus * 100, 2) if implied_guide and q2_consensus else None,
            "year_ago_quarter_revenue": ya_rev,
            "yoy_pct": round((beat_adjusted_q1 - ya_rev) / ya_rev * 100, 1) if beat_adjusted_q1 and ya_rev else None,
        },
        "beat_cadence": {
            "selected_beat_pct": selected_beat,
            "selected_window": selected_window,
            "avg_4q": beat_4q_avg,
            "avg_8q": beat_8q_avg,
            "history": beat_history,
        },
        "momentum": momentum,
        "qoq_history": qoq_history,
        "anomalies": anomalies,
        "transcript_analyses": {
            "latest_period": latest_ta_period,
            "latest_text": latest_ta[:2000] if latest_ta else None,
            "count": len(transcript_analyses),
        },
        "latest_agent_report": latest_report[:3000] if latest_report else None,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python earnings_prep.py TICKER")
        sys.exit(1)
    data = fetch_earnings_data(sys.argv[1].upper())
    print(json.dumps(data, indent=2, default=str))
