import psycopg2
import anthropic
from dotenv import load_dotenv
import os
import json
import statistics
import calendar
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from collections import defaultdict

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

TICKERS = [
    "SNOW", "DDOG", "MDB", "TENB", "QLYS",
    "NOW", "ADBE", "INTU", "WDAY", "PANW",
    "CDNS", "SNPS", "ADSK", "APP", "FTNT",
    "TEAM", "VEEV", "ROP", "PLTR",
    "HUBS", "ZS", "CRWD", "OKTA", "TTD",
    "GTLB", "BILL", "MNDY", "CFLT", "ESTC",
    "FROG", "S", "ZI", "DT", "TOST",
    "PCTY", "PAYC", "GWRE", "BSY", "CWAN",
    "NCNO", "BRZE", "KVYO", "PCOR", "SPSC",
    "MANH", "AZPN", "APPN", "DOCN",
]


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        port=5432,
        sslmode="require"
    )


def quarter_from_date(date_str):
    month = datetime.strptime(date_str, "%Y-%m-%d").month
    return (month - 1) // 3 + 1


def quarter_end_date(year, quarter):
    month = quarter * 3
    last_day = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-{last_day:02d}"


def next_period(period_str, steps=1):
    """Advance a period date by `steps` quarters (3 months each).

    Preserves fiscal calendar: uses the last day of the target month,
    so SNOW's Jan-31 advances to Apr-30, Jul-31, Oct-31, Jan-31.
    """
    d = datetime.strptime(period_str, "%Y-%m-%d")
    for _ in range(steps):
        m = d.month + 3
        y = d.year
        if m > 12:
            m -= 12
            y += 1
        d = datetime(y, m, calendar.monthrange(y, m)[1])
    return d.strftime("%Y-%m-%d")


def get_db_data(ticker):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT period, revenue FROM revenue_actuals WHERE ticker=%s ORDER BY period ASC",
        (ticker,)
    )
    actuals = [{"period": str(r[0]), "revenue": r[1]} for r in cur.fetchall()]

    cur.execute(
        "SELECT period, estimated_revenue FROM consensus_estimates WHERE ticker=%s ORDER BY period ASC",
        (ticker,)
    )
    estimates = [{"period": str(r[0]), "estimated_revenue": r[1]} for r in cur.fetchall()]

    cur.close()
    conn.close()
    return actuals, estimates


def supplement_estimates_from_earnings(ticker, estimates, actuals):
    """Pull near-term estimates from FMP /earnings that /analyst-estimates misses."""
    import requests as req
    url = f"https://financialmodelingprep.com/stable/earnings?symbol={ticker}&apikey={os.getenv('FMP_API_KEY')}"
    data = req.get(url).json()
    if not isinstance(data, list):
        return estimates

    actual_periods = {a["period"] for a in actuals}
    est_periods = {e["period"] for e in estimates}
    last_period = actuals[-1]["period"]

    supplemented = list(estimates)
    for e in data:
        if e.get("revenueActual") is not None:
            continue
        re = e.get("revenueEstimated")
        if not re or re <= 0:
            continue
        earnings_date = e.get("date", "")
        if not earnings_date:
            continue
        earn_d = datetime.strptime(earnings_date, "%Y-%m-%d")
        for steps in range(1, 5):
            per = next_period(last_period, steps)
            per_d = datetime.strptime(per, "%Y-%m-%d")
            gap = (earn_d - per_d).days
            if 0 < gap < 90 and per not in actual_periods and per not in est_periods:
                supplemented.append({"period": per, "estimated_revenue": re})
                est_periods.add(per)
                break

    return supplemented


def get_transcript_analyses(ticker):
    """Fetch transcript analyses for anomalous quarters from the DB."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT period, transcript_analysis FROM transcripts "
        "WHERE ticker=%s AND transcript_analysis IS NOT NULL",
        (ticker,)
    )
    results = {str(r[0]): r[1] for r in cur.fetchall()}
    cur.close()
    conn.close()
    return results


def compute_qoq(actuals):
    results = []
    for i in range(1, len(actuals)):
        prev_rev = actuals[i - 1]["revenue"]
        curr_rev = actuals[i]["revenue"]
        dollar_change = curr_rev - prev_rev
        pct_change = (dollar_change / prev_rev * 100) if prev_rev else 0
        q = quarter_from_date(actuals[i]["period"])
        results.append({
            "period": actuals[i]["period"],
            "quarter": f"Q{q}",
            "revenue": curr_rev,
            "prev_revenue": prev_rev,
            "qoq_dollar_change": dollar_change,
            "qoq_pct_change": round(pct_change, 2)
        })
    return results


def compute_seasonality(qoq_data):
    """Compute company-specific seasonal baselines from last 8 quarters per season.

    For each fiscal quarter (Q1-Q4), computes mean, stdev, and CV of $ QoQ
    from the company's own history. CV > 0.4 triggers exponential decay weighting.
    """
    by_q = defaultdict(list)
    for row in qoq_data:
        by_q[row["quarter"]].append(row["qoq_dollar_change"])

    seasonal = {}
    for q in ["Q1", "Q2", "Q3", "Q4"]:
        all_changes = by_q.get(q, [])
        if not all_changes:
            continue
        # Use last 8 observations for this season
        changes = all_changes[-8:]
        n = len(changes)
        avg = statistics.mean(changes)
        std = statistics.stdev(changes) if n > 1 else 0
        cv = abs(std / avg) if avg != 0 else 0

        # High-CV companies: exponential decay weighting (decay=0.85)
        if cv > 0.4 and n >= 2:
            decay = 0.85
            weights = [decay ** (n - 1 - i) for i in range(n)]
            w_avg = sum(v * w for v, w in zip(changes, weights)) / sum(weights)
        else:
            w_avg = avg

        seasonal[q] = {
            "avg_qoq_change": round(w_avg),
            "std_qoq_change": round(std),
            "observations": n,
            "cv": round(cv, 3),
            "weighting": "exponential_decay" if cv > 0.4 and n >= 2 else "equal",
        }
    return seasonal


def flag_anomalies(qoq_data, seasonal):
    flagged = []
    for row in qoq_data:
        q = row["quarter"]
        if q in seasonal and seasonal[q]["std_qoq_change"] > 0:
            dev = abs(row["qoq_dollar_change"] - seasonal[q]["avg_qoq_change"]) / seasonal[q]["std_qoq_change"]
            if dev > 1.5:
                flagged.append({
                    "period": row["period"],
                    "quarter": q,
                    "actual_qoq_change": row["qoq_dollar_change"],
                    "seasonal_avg": seasonal[q]["avg_qoq_change"],
                    "std_deviations": round(dev, 2)
                })
    return flagged


def classify_seasonal_trend(values, cv=0.0):
    """Classify $ QoQ trend for a seasonal quarter using Kinetic decision tree.

    Uses company-specific history (last 8 quarters of the same season).
    High-CV seasons (>0.4) use exponential decay weighting for the volatile case.
    """
    if not values:
        return "no_data", 0
    if len(values) == 1:
        return "insufficient", values[0]

    n = len(values)
    last = values[-1]
    diffs = [values[i] - values[i - 1] for i in range(1, n)]

    mean_val = statistics.mean(values)
    std_val = statistics.stdev(values)
    if mean_val != 0 and abs(std_val / mean_val) < 0.10:
        return "flat", last

    up = sum(1 for d in diffs if d > 0)
    down = sum(1 for d in diffs if d < 0)
    total = len(diffs)

    if up / total >= 0.6:
        recent = diffs[-min(3, len(diffs)):]
        if len(recent) >= 2:
            accel = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
            if all(a > 0 for a in accel):
                return "accelerating", round(last * 1.03)
            if all(a < 0 for a in accel):
                return "decelerating", round(last * 0.92)
        return "growing", last

    if down / total >= 0.6:
        recent_diffs = diffs[-min(3, len(diffs)):]
        avg_decline = statistics.mean(recent_diffs)
        return "declining", round(last + avg_decline)

    # Volatile/mixed — use exponential decay for high-CV companies
    if cv > 0.4 and n >= 2:
        decay = 0.85
        weights = [decay ** (n - 1 - i) for i in range(n)]
    else:
        weights = list(range(1, n + 1))
    weighted = sum(v * w for v, w in zip(values, weights)) / sum(weights)
    return "volatile", round(weighted)


def compute_momentum(qoq_data):
    """Analyze last 2-3 quarters of $ QoQ regardless of seasonality."""
    recent = [r["qoq_dollar_change"] for r in qoq_data[-3:]]
    if len(recent) < 2:
        return "neutral", 1.0, recent
    diffs = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
    if all(d > 0 for d in diffs):
        return "accelerating", 1.03, recent
    if all(d < 0 for d in diffs):
        return "decelerating", 0.97, recent
    return "stable", 1.0, recent


def compute_qoq_yoy(qoq_data):
    """Compute YoY change in $ QoQ to contextualize growth rate."""
    by_yq = {}
    for row in qoq_data:
        d = datetime.strptime(row["period"], "%Y-%m-%d")
        by_yq[(d.year, row["quarter"])] = row["qoq_dollar_change"]
    results = []
    for (year, q), val in sorted(by_yq.items()):
        prior = by_yq.get((year - 1, q))
        if prior is not None and prior != 0:
            change = val - prior
            pct = round(change / abs(prior) * 100, 1)
            results.append({
                "period": f"{year} {q}",
                "qoq_current": val,
                "qoq_prior_year": prior,
                "yoy_change_dollars": change,
                "yoy_change_pct": pct
            })
    return results


def compute_beat_cadence(ticker):
    """Compute historical beat pattern from FMP earnings endpoint."""
    import requests
    url = f"https://financialmodelingprep.com/stable/earnings?symbol={ticker}&apikey={os.getenv('FMP_API_KEY')}"
    data = requests.get(url).json()

    if not isinstance(data, list):
        return None

    beats = []
    for e in data:
        ra = e.get("revenueActual")
        re = e.get("revenueEstimated")
        if ra and re and re > 0:
            beat_pct = (ra - re) / re * 100
            beats.append({
                "date": e.get("date", ""),
                "actual": ra,
                "estimate": re,
                "beat_pct": round(beat_pct, 2)
            })

    if len(beats) < 2:
        return None

    last_4 = [b["beat_pct"] for b in beats[:4]]
    last_8 = [b["beat_pct"] for b in beats[:8]]

    avg_4 = statistics.mean(last_4)
    avg_8 = statistics.mean(last_8)
    std_4 = statistics.stdev(last_4) if len(last_4) > 1 else float("inf")
    std_8 = statistics.stdev(last_8) if len(last_8) > 1 else float("inf")

    if std_4 <= std_8:
        selected, selected_window = avg_4, "4Q"
    else:
        selected, selected_window = avg_8, "8Q"

    return {
        "avg_beat_4q": round(avg_4, 2),
        "avg_beat_8q": round(avg_8, 2),
        "std_4q": round(std_4, 2) if std_4 != float("inf") else 0,
        "std_8q": round(std_8, 2) if std_8 != float("inf") else 0,
        "selected_beat_pct": round(selected, 2),
        "selected_window": selected_window,
        "is_changing": abs(avg_4 - avg_8) > 1.5,
        "recent_beats": beats[:8]
    }


# ── projection framework ─────────────────────────────────────────────────


def compute_forward_adjustments(anomalies, transcript_analyses):
    """Derive forward-looking adjustments from anomaly detection and transcript analysis.

    Returns a dict of modifiers to apply to the next quarter's $ QoQ projection:
      deal_clustering_haircut: -0.08 if prior quarter had deal-clustering anomaly
      nrr_modifier: +0.03 if NRR improving, -0.03 if declining, 0 otherwise
    """
    adjustments = {"deal_clustering_haircut": 0.0, "nrr_modifier": 0.0, "flags": []}

    # Check most recent anomaly for deal-clustering signals
    if anomalies:
        latest = anomalies[-1]
        # Deal clustering: anomalous quarter where actual >> seasonal avg (positive spike)
        if latest["actual_qoq_change"] > 0 and latest["std_deviations"] > 1.5:
            commentary = latest.get("management_commentary", "")
            if commentary:
                lower = commentary.lower()
                if any(kw in lower for kw in [
                    "deal", "large contract", "pull-forward", "lumpy",
                    "clustering", "one-time", "catch-up", "back-loaded"
                ]):
                    adjustments["deal_clustering_haircut"] = -0.08
                    adjustments["flags"].append(
                        f"deal_clustering ({latest['period']}): -8% haircut")

    # Check most recent transcript analysis for NRR/expansion signals
    if transcript_analyses:
        # Get the most recent analysis
        latest_period = max(transcript_analyses.keys())
        latest_ta = transcript_analyses[latest_period].lower()

        nrr_improving = any(kw in latest_ta for kw in [
            "net retention improv", "nrr improv", "expansion rate increas",
            "net expansion improv", "nrr above", "net retention above",
            "upsell momentum", "expansion accelerat",
        ])
        nrr_declining = any(kw in latest_ta for kw in [
            "net retention declin", "nrr declin", "expansion rate decreas",
            "net expansion declin", "nrr below", "net retention below",
            "downsell", "contraction", "churn increas",
        ])

        if nrr_improving:
            adjustments["nrr_modifier"] = 0.03
            adjustments["flags"].append(f"nrr_improving ({latest_period}): +3%")
        elif nrr_declining:
            adjustments["nrr_modifier"] = -0.03
            adjustments["flags"].append(f"nrr_declining ({latest_period}): -3%")

    return adjustments


MAX_FWD_DATE = datetime(2028, 1, 31)  # Show up to FY ending closest to Dec 2027


def quarters_to_cutoff(last_period):
    """Count how many quarters from last_period to MAX_FWD_DATE."""
    n = 0
    per = last_period
    while True:
        per = next_period(per, 1)
        if datetime.strptime(per, "%Y-%m-%d") > MAX_FWD_DATE:
            break
        n += 1
    return max(n, 4)


def extrapolate(actuals, qoq_data, estimates, beat_cadence, seasonal, anomalies=None, transcript_analyses=None, n=None):
    """Project forward n quarters using Kinetic methodology.

    Q+1: consensus x (1 + avg beat %) — anchored to sell-side estimates.
    Q+2 through Q+n: $ QoQ decision tree chained off beat-adjusted Q+1,
        using company-specific seasonal baselines with CV-aware weighting
        and forward-looking adjustments from transcript analysis.
    Falls back to pure $ QoQ if no beat cadence or no Q+1 consensus.
    """
    if n is None:
        n = quarters_to_cutoff(actuals[-1]["period"])
    by_q = defaultdict(list)
    for row in qoq_data:
        by_q[row["quarter"]].append(row["qoq_dollar_change"])

    seasonal_forecasts = {}
    for q in ["Q1", "Q2", "Q3", "Q4"]:
        all_values = by_q.get(q, [])
        if not all_values:
            continue
        # Company-specific: use last 8 observations for this season
        values = all_values[-8:]
        cv = seasonal[q]["cv"] if q in seasonal else 0
        trend, projected = classify_seasonal_trend(values, cv=cv)
        seasonal_forecasts[q] = {
            "trend": trend,
            "projected_qoq": projected,
            "history": values,
            "cv": cv,
            "weighting": seasonal[q].get("weighting", "equal") if q in seasonal else "equal",
        }

    momentum_label, momentum_factor, recent_qoq = compute_momentum(qoq_data)
    qoq_yoy = compute_qoq_yoy(qoq_data)

    # Forward-looking adjustments from anomaly detection + transcripts
    fwd_adj = compute_forward_adjustments(
        anomalies or [], transcript_analyses or {})
    total_fwd_modifier = 1.0 + fwd_adj["deal_clustering_haircut"] + fwd_adj["nrr_modifier"]

    last_period = actuals[-1]["period"]
    actual_periods = {a["period"] for a in actuals}
    actual_yqs = set()
    for a in actuals:
        ad = datetime.strptime(a["period"], "%Y-%m-%d")
        actual_yqs.add((ad.year, quarter_from_date(a["period"])))

    beat_pct = (beat_cadence["selected_beat_pct"] / 100) if beat_cadence else 0

    # Build consensus lookup by (year, quarter)
    est_by_yq = {}
    for e in estimates:
        if e["estimated_revenue"]:
            ed = datetime.strptime(e["period"], "%Y-%m-%d")
            yq = (ed.year, quarter_from_date(e["period"]))
            if yq not in actual_yqs:
                est_by_yq[yq] = e["estimated_revenue"]

    projections = []
    prev_rev = actuals[-1]["revenue"]

    for i in range(1, n + 1):
        period = next_period(last_period, i)
        proj_q = quarter_from_date(period)
        proj_year = datetime.strptime(period, "%Y-%m-%d").year
        q_key = f"Q{proj_q}"

        consensus_match = est_by_yq.get((proj_year, proj_q))

        if i == 1 and consensus_match and beat_cadence:
            proj_rev = round(consensus_match * (1 + beat_pct))
            method = "beat_adjusted"
            trend_used = "beat_adjusted"
        else:
            if q_key in seasonal_forecasts:
                base_qoq = seasonal_forecasts[q_key]["projected_qoq"]
                trend_used = seasonal_forecasts[q_key]["trend"]
            else:
                base_qoq = 0
                trend_used = "no_data"
            # Apply momentum overlay
            adjusted_qoq = round(base_qoq * momentum_factor)
            # Apply forward-looking adjustment (deal clustering, NRR)
            if fwd_adj["flags"]:
                adjusted_qoq = round(adjusted_qoq * total_fwd_modifier)
            proj_rev = prev_rev + adjusted_qoq
            method = "qoq_extrapolation"

        variance_pct = None
        if consensus_match:
            variance_pct = round((proj_rev - consensus_match) / consensus_match * 100, 2)

        projections.append({
            "period": period,
            "quarter": q_key,
            "projected_revenue": proj_rev,
            "projected_qoq": proj_rev - prev_rev,
            "seasonal_trend": trend_used,
            "momentum": momentum_label,
            "method": method,
            "consensus": consensus_match,
            "variance_pct": variance_pct,
        })
        prev_rev = proj_rev

    return projections, seasonal_forecasts, momentum_label, momentum_factor, qoq_yoy, fwd_adj


def build_guide_inference(projections, beat_cadence):
    """Build implied guide for Q+2 only — the first unguided quarter.

    Implied guide = Q+2 projected actual / (1 + avg beat %).
    """
    if not beat_cadence or len(projections) < 2:
        return None

    beat_pct = beat_cadence["selected_beat_pct"] / 100
    q2 = projections[1]  # Q+2

    implied_guide = round(q2["projected_revenue"] / (1 + beat_pct))
    consensus = q2.get("consensus")

    gap_dollars = round(implied_guide - consensus) if consensus else None
    gap_pct = round((implied_guide - consensus) / consensus * 100, 2) if consensus else None

    gap_signal = None
    if gap_pct is not None:
        if gap_pct > 2:
            gap_signal = "GUIDE ABOVE"
        elif gap_pct < -2:
            gap_signal = "GUIDE BELOW"
        else:
            gap_signal = "GUIDE IN-LINE"

    return {
        "period": q2["period"],
        "quarter": q2["quarter"],
        "projected_actual": q2["projected_revenue"],
        "implied_guide": implied_guide,
        "consensus": consensus,
        "gap_dollars": gap_dollars,
        "gap_pct": gap_pct,
        "signal": gap_signal,
        "beat_cadence": beat_cadence,
    }


def consensus_comparison(actuals, projections, estimates):
    today = datetime.now()
    current_fy = today.year
    next_fy = current_fy + 1

    next_q_comp = None
    if projections:
        p = projections[0]
        next_q_comp = {
            "period": p["period"],
            "projection": p["projected_revenue"],
            "consensus": p["consensus"],
            "diff_dollars": p["projected_revenue"] - p["consensus"] if p["consensus"] else None,
            "diff_pct": p["variance_pct"],
            "signal": "BEAT" if (p["variance_pct"] or 0) > 0 else "MISS",
            "method": p["method"],
        } if p["consensus"] else None

    def fy_totals(year):
        act_total = sum(a["revenue"] for a in actuals
                        if datetime.strptime(a["period"], "%Y-%m-%d").year == year)
        proj_total = sum(p["projected_revenue"] for p in projections
                         if datetime.strptime(p["period"], "%Y-%m-%d").year == year)
        est_total = sum(e["estimated_revenue"] for e in estimates
                        if datetime.strptime(e["period"], "%Y-%m-%d").year == year)
        if est_total == 0:
            return None
        our_total = act_total + proj_total
        diff = our_total - est_total
        return {
            "fiscal_year": year,
            "our_total": our_total,
            "consensus_total": est_total,
            "diff_dollars": diff,
            "diff_pct": round(diff / est_total * 100, 2),
            "signal": "BEAT" if diff > 0 else "MISS"
        }

    return next_q_comp, fy_totals(current_fy), fy_totals(next_fy)


# ── agent execution ──────────────────────────────────────────────────────


def run_agent(ticker):
    actuals, estimates = get_db_data(ticker)

    if not actuals:
        print(f"No data found for {ticker}")
        return

    estimates = supplement_estimates_from_earnings(ticker, estimates, actuals)
    qoq = compute_qoq(actuals)
    seasonal = compute_seasonality(qoq)
    anomalies = flag_anomalies(qoq, seasonal)
    beat_cadence = compute_beat_cadence(ticker)

    # Enrich anomalies with transcript-based management commentary (needed before extrapolate)
    transcript_analyses = get_transcript_analyses(ticker)
    for anomaly in anomalies:
        ta = transcript_analyses.get(anomaly["period"])
        if ta:
            anomaly["management_commentary"] = ta

    projections, seasonal_forecasts, momentum_label, momentum_factor, qoq_yoy, fwd_adj = \
        extrapolate(actuals, qoq, estimates, beat_cadence, seasonal,
                    anomalies=anomalies, transcript_analyses=transcript_analyses, n=4)

    guide_inference = build_guide_inference(projections, beat_cadence)
    next_q, current_fy, next_fy = consensus_comparison(actuals, projections, estimates)

    current_fy_year = current_fy["fiscal_year"] if current_fy else datetime.now().year
    next_fy_year = next_fy["fiscal_year"] if next_fy else datetime.now().year + 1

    analysis = {
        "ticker": ticker,
        "quarterly_actuals_count": len(actuals),
        "latest_quarter": actuals[-1] if actuals else None,
        "qoq_changes_last_8": qoq[-8:],
        "seasonal_patterns": {
            q: {**v, "cv": v["cv"], "weighting": v["weighting"]}
            for q, v in seasonal.items()
        },
        "seasonal_forecasts": {
            q: {"trend": v["trend"], "projected_qoq": v["projected_qoq"],
                "cv": v.get("cv", 0), "weighting": v.get("weighting", "equal")}
            for q, v in seasonal_forecasts.items()
        },
        "momentum": {
            "label": momentum_label,
            "factor": momentum_factor,
            "recent_qoq": [r["qoq_dollar_change"] for r in qoq[-3:]]
        },
        "forward_adjustments": {
            "total_modifier": round(1.0 + fwd_adj["deal_clustering_haircut"] + fwd_adj["nrr_modifier"], 3),
            "deal_clustering_haircut": fwd_adj["deal_clustering_haircut"],
            "nrr_modifier": fwd_adj["nrr_modifier"],
            "flags": fwd_adj["flags"],
        },
        "qoq_yoy": qoq_yoy[-8:] if qoq_yoy else [],
        "anomalous_quarters": anomalies,
        "forward_projections": projections,
        "beat_cadence": beat_cadence,
        "guide_inference_q2": guide_inference,
        "consensus_comparison": {
            "next_quarter": next_q,
            "current_fy": current_fy,
            "next_fy": next_fy
        }
    }

    prompt = f"""You are a senior financial analyst at a hedge fund covering high-growth software companies.

Below is pre-computed quarterly revenue analysis for {ticker}. All numbers are in USD.

## Forecasting Methodology

**Q+1 (next reported quarter):** Uses consensus estimate as guide proxy, multiplied by (1 + avg beat %) to arrive at the implied actual. This anchors the near-term projection to sell-side estimates rather than pure extrapolation.

**Q+2 through Q+4:** Uses the $ QoQ (dollar quarter-over-quarter change) decision tree, chained off the beat-adjusted Q+1 as base:
- consistently growing -> project off last datapoint (not the average)
- accelerating -> hold or slightly increase last datapoint
- decelerating -> apply modest haircut
- flat -> hold last datapoint
- volatile/mixed -> exponential-decay-weighted average (if CV > 40%) or recency-weighted average
- declining -> extrapolate the decline

**Company-specific seasonality:** Each seasonal quarter (Q1-Q4) is baselined from the company's own last 8 quarters of that season. The coefficient of variation (CV) per season is computed. If CV exceeds 40%, exponential decay weighting (factor 0.85) is applied so recent data dominates.

**Forward-looking adjustments:** If transcript analysis flags deal-clustering in the prior quarter, an 8% haircut is applied to the next $ QoQ. If NRR/net expansion is flagged as improving, a +3% modifier is applied; if declining, -3%.

A momentum overlay from the last 2-3 quarters of $ QoQ (regardless of seasonality) adjusts projections.

**Implied Guide (Q+2 only):** implied_guide = Q+2 projected actual / (1 + avg beat %). This infers what management will likely guide to for the first unguided quarter.

{json.dumps(analysis, indent=2)}

Write a concise research note with these sections:

1. **$ QoQ Revenue Trend** — Summarize recent $ QoQ changes and momentum. Note acceleration or deceleration across both seasonal and cross-quarter perspectives.

2. **Seasonal $ QoQ Analysis** — For each quarter (Q1-Q4), interpret the trend classification, the projected $ QoQ, and how it compares to history. Note the CV and weighting method per season. Call out which quarter is strongest/weakest and whether any season has high variability (CV > 40%).

3. **Momentum Overlay** — Interpret the cross-seasonal momentum signal. How do the last 2-3 quarters of $ QoQ inform the near-term outlook?

4. **YoY Change in $ QoQ** — Interpret how $ QoQ is changing year-over-year. Is the business adding more or less sequentially than a year ago?

5. **Anomalous Quarters & Management Commentary** — For each flagged anomaly, explain the likely driver and whether it should be weighted in forward estimates. Where a `management_commentary` field is present, cite the key factors and state whether the distortion is one-time or structural.

6. **Forward-Looking Adjustments** — Report any active adjustments (deal clustering haircut, NRR modifier) with the total modifier applied. If no adjustments, state that.

7. **4-Quarter Forward Projection** — Present the projection in a table (period, quarter, projected revenue, projected $ QoQ, method, trend/momentum). Clearly note that Q+1 uses beat-adjusted consensus while Q+2-Q+4 use $ QoQ chained off Q+1. Show the variance vs consensus for each quarter.

8. **Beat Cadence & Q+2 Implied Guide** — Report the historical beat % (4Q vs 8Q, which was selected, is it changing?). Present the Q+1 beat-adjusted implied actual. Then show the Q+2 implied guide, its variance vs consensus, and the GUIDE ABOVE/BELOW/IN-LINE signal.

9. **Consensus Comparison** —
   - Next quarter: beat/miss signal with $ and % gap
   - Current FY ({current_fy_year}): beat/miss with total gap
   - Next FY ({next_fy_year}): beat/miss with total gap
   Flag divergences >5% as high-conviction signals.

10. **Investment Implication** — 2-3 sentences on positioning. Be specific with numbers. Reference the Q+2 guide inference signal and any forward-looking adjustments.

Use exact numbers from the data. Format with headers and tables."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3500,
        messages=[{"role": "user", "content": prompt}]
    )

    report = message.content[0].text

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO agent_reports (ticker, report) VALUES (%s, %s)", (ticker, report))
    conn.commit()
    cur.close()
    conn.close()

    print(f"\n{'='*60}")
    print(f"  {ticker} — Quarterly Revenue Analysis")
    print(f"{'='*60}")
    print(report.encode("ascii", errors="replace").decode("ascii"))


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(run_agent, t): t for t in TICKERS}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                future.result()
            except Exception as e:
                print(f"\nERROR processing {ticker}: {e}")
    print("\nAll reports complete and saved to database.")
