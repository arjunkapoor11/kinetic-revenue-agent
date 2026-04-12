"""
Kinetic Revenue Agent — Slack Notification

Reads the most recent pipeline results from RDS and posts a summary
to the #software-dashboard Slack channel via webhook.

Can be run standalone: python slack_notify.py
"""

import json
import os
import psycopg2
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK")
CHANNEL = "#software-dashboard"


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"), database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"),
        port=5432, sslmode="require")


def get_guide_signals():
    """Read the latest guide inference data for all tickers from the DB.

    Uses the export module's analytics to compute current guide signals
    without making any API calls — reads only from DB.
    """
    conn = get_db_connection()
    cur = conn.cursor()

    # Get all tickers that have actuals
    cur.execute("SELECT DISTINCT ticker FROM revenue_actuals ORDER BY ticker")
    tickers = [r[0] for r in cur.fetchall()]

    # Get latest actuals per ticker
    cur.execute("""
        SELECT ticker, period, revenue
        FROM revenue_actuals
        ORDER BY ticker, period
    """)
    actuals_by_tk = {}
    for tk, per, rev in cur.fetchall():
        actuals_by_tk.setdefault(tk, []).append({"period": str(per), "revenue": rev})

    # Get consensus estimates
    cur.execute("""
        SELECT ticker, period, estimated_revenue
        FROM consensus_estimates
        ORDER BY ticker, period
    """)
    estimates_by_tk = {}
    for tk, per, est in cur.fetchall():
        estimates_by_tk.setdefault(tk, []).append({"period": str(per), "estimated_revenue": est})

    # Get pre-earnings consensus for beat cadence
    cur.execute("""
        SELECT ticker, period, estimated_revenue
        FROM pre_earnings_consensus
        ORDER BY ticker, period
    """)
    pec_by_tk = {}
    for tk, per, est in cur.fetchall():
        pec_by_tk.setdefault(tk, {})[str(per)] = est

    cur.close()
    conn.close()

    # Compute guide signals using the same logic as export.py
    import statistics
    from collections import defaultdict

    def quarter_from_date(s):
        return (datetime.strptime(s, "%Y-%m-%d").month - 1) // 3 + 1

    signals = []

    for tk in tickers:
        acts = actuals_by_tk.get(tk, [])
        ests = estimates_by_tk.get(tk, [])
        pec = pec_by_tk.get(tk, {})

        if len(acts) < 8:
            continue

        # Beat cadence from pre-earnings consensus
        beats = []
        for a in reversed(acts):
            est = pec.get(a["period"])
            if est and est > 0:
                beats.append((a["revenue"] - est) / est * 100)
            if len(beats) >= 8:
                break

        if len(beats) < 2:
            continue

        l4 = beats[:4]
        l8 = beats[:8]
        s4 = statistics.stdev(l4) if len(l4) > 1 else float("inf")
        s8 = statistics.stdev(l8) if len(l8) > 1 else float("inf")
        beat_pct = (statistics.mean(l4) if s4 <= s8 else statistics.mean(l8)) / 100

        # Find Q+1 consensus
        actual_periods = {a["period"] for a in acts}
        actual_yqs = set()
        for a in acts:
            d = datetime.strptime(a["period"], "%Y-%m-%d")
            actual_yqs.add((d.year, quarter_from_date(a["period"])))

        est_by_yq = {}
        for e in ests:
            if e["estimated_revenue"]:
                d = datetime.strptime(e["period"], "%Y-%m-%d")
                yq = (d.year, quarter_from_date(e["period"]))
                if yq not in actual_yqs:
                    est_by_yq[yq] = e["estimated_revenue"]

        # Q+1: beat-adjusted
        last_per = acts[-1]["period"]
        last_d = datetime.strptime(last_per, "%Y-%m-%d")
        # Next quarter
        m = last_d.month + 3
        y = last_d.year
        if m > 12:
            m -= 12
            y += 1
        import calendar
        next_per_d = datetime(y, m, calendar.monthrange(y, m)[1])
        next_q = quarter_from_date(next_per_d.strftime("%Y-%m-%d"))
        q1_con = est_by_yq.get((next_per_d.year, next_q))

        if not q1_con:
            continue

        q1_rev = round(q1_con * (1 + beat_pct))
        q1_var = (q1_rev - q1_con) / q1_con * 100

        # Q+2 for guide inference
        m2 = next_per_d.month + 3
        y2 = next_per_d.year
        if m2 > 12:
            m2 -= 12
            y2 += 1
        q2_d = datetime(y2, m2, calendar.monthrange(y2, m2)[1])
        q2_q = quarter_from_date(q2_d.strftime("%Y-%m-%d"))
        q2_con = est_by_yq.get((q2_d.year, q2_q))

        # Simple Q+2 projection: Q+1 + avg recent $ QoQ
        recent_qoq = []
        for i in range(max(1, len(acts) - 4), len(acts)):
            recent_qoq.append(acts[i]["revenue"] - acts[i - 1]["revenue"])
        avg_qoq = statistics.mean(recent_qoq) if recent_qoq else 0
        q2_rev = q1_rev + round(avg_qoq)
        q2_guide = round(q2_rev / (1 + beat_pct))

        gap_pct = None
        signal = None
        if q2_con and q2_con > 0:
            gap_pct = round((q2_guide - q2_con) / q2_con * 100, 1)
            if gap_pct > 2:
                signal = "GUIDE ABOVE"
            elif gap_pct < -2:
                signal = "GUIDE BELOW"
            else:
                signal = "IN-LINE"

        # Latest trailing revenue for sorting
        trailing_rev = sum(a["revenue"] for a in acts[-4:])

        signals.append({
            "ticker": tk,
            "signal": signal,
            "gap_pct": gap_pct,
            "beat_pct": round(beat_pct * 100, 2),
            "q1_rev_m": round(q1_rev / 1e6, 1),
            "q1_con_m": round(q1_con / 1e6, 1),
            "q2_guide_m": round(q2_guide / 1e6, 1) if q2_con else None,
            "q2_con_m": round(q2_con / 1e6, 1) if q2_con else None,
            "trailing_rev": trailing_rev,
        })

    return signals


def build_slack_message(signals):
    """Build Slack message blocks from guide signals."""
    today = datetime.now().strftime("%B %d, %Y")

    above = sorted([s for s in signals if s["signal"] == "GUIDE ABOVE"],
                    key=lambda x: x["gap_pct"], reverse=True)
    below = sorted([s for s in signals if s["signal"] == "GUIDE BELOW"],
                    key=lambda x: x["gap_pct"])
    inline = [s for s in signals if s["signal"] == "IN-LINE"]
    no_signal = [s for s in signals if s["signal"] is None]

    lines = []
    lines.append(f":chart_with_upwards_trend: *Kinetic Revenue Pipeline — {today}*")
    lines.append(f"_{len(signals)} tickers analyzed | {len(above)} GUIDE ABOVE | {len(below)} GUIDE BELOW | {len(inline)} IN-LINE_")
    lines.append("")

    if above:
        lines.append("*:arrow_up: GUIDE ABOVE* (implied guide > consensus by >2%)")
        for s in above:
            lines.append(
                f"  `{s['ticker']:5s}`  gap={s['gap_pct']:+.1f}%  "
                f"guide=${s['q2_guide_m']}M vs con=${s['q2_con_m']}M  "
                f"beat={s['beat_pct']:.1f}%")
        lines.append("")

    if below:
        lines.append("*:arrow_down: GUIDE BELOW* (implied guide < consensus by >2%)")
        for s in below:
            lines.append(
                f"  `{s['ticker']:5s}`  gap={s['gap_pct']:+.1f}%  "
                f"guide=${s['q2_guide_m']}M vs con=${s['q2_con_m']}M  "
                f"beat={s['beat_pct']:.1f}%")
        lines.append("")

    if inline:
        tk_list = ", ".join(s["ticker"] for s in sorted(inline, key=lambda x: x["ticker"]))
        lines.append(f"*:white_check_mark: IN-LINE* ({len(inline)}): {tk_list}")
        lines.append("")

    lines.append("_Full Excel model: kinetic_revenue_model.xlsx on EC2_")

    return "\n".join(lines)


def post_to_slack(message):
    """Post a message to Slack via webhook."""
    if not SLACK_WEBHOOK:
        print("[slack] No SLACK_WEBHOOK configured — printing message only")
        print(message)
        return False

    payload = {
        "channel": CHANNEL,
        "text": message,
        "unfurl_links": False,
    }

    resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
    if resp.status_code == 200 and resp.text == "ok":
        print(f"[slack] Posted to {CHANNEL}")
        return True
    else:
        print(f"[slack] Failed: {resp.status_code} {resp.text}")
        return False


def build_and_post():
    """Full pipeline: read DB, build message, post to Slack."""
    signals = get_guide_signals()
    message = build_slack_message(signals)
    print(message)
    return post_to_slack(message)


if __name__ == "__main__":
    build_and_post()
