import argparse
import psycopg2
from dotenv import load_dotenv
import os

from credentials import load_credentials, add_credentials_args
import json
import statistics
import calendar
import webbrowser
from datetime import datetime
from collections import defaultdict

load_dotenv()

TICKERS = ["SNOW", "DDOG", "MDB", "TENB", "QLYS"]


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


def get_db_data(ticker):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT period, revenue FROM revenue_actuals WHERE ticker=%s ORDER BY period ASC",
        (ticker,),
    )
    actuals = [{"period": str(r[0]), "revenue": r[1]} for r in cur.fetchall()]
    cur.execute(
        "SELECT period, estimated_revenue FROM consensus_estimates WHERE ticker=%s ORDER BY period ASC",
        (ticker,),
    )
    estimates = [{"period": str(r[0]), "estimated_revenue": r[1]} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return actuals, estimates


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
            "qoq_dollar_change": dollar_change,
            "qoq_pct_change": round(pct_change, 2),
        })
    return results


def compute_seasonality(qoq_data):
    by_q = defaultdict(list)
    for row in qoq_data:
        by_q[row["quarter"]].append(row["qoq_dollar_change"])
    seasonal = {}
    for q in ["Q1", "Q2", "Q3", "Q4"]:
        changes = by_q.get(q, [])
        if changes:
            avg = statistics.mean(changes)
            std = statistics.stdev(changes) if len(changes) > 1 else 0
            seasonal[q] = {
                "avg_qoq_change": round(avg),
                "std_qoq_change": round(std),
                "observations": len(changes),
            }
    return seasonal


def flag_anomalies(qoq_data, seasonal):
    flagged = []
    for row in qoq_data:
        q = row["quarter"]
        if q in seasonal and seasonal[q]["std_qoq_change"] > 0:
            dev = abs(row["qoq_dollar_change"] - seasonal[q]["avg_qoq_change"]) / seasonal[q]["std_qoq_change"]
            if dev > 1.5:
                flagged.append(row["period"])
    return flagged


def classify_seasonal_trend(values):
    """Classify $ QoQ trend for a seasonal quarter using Kinetic decision tree."""
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


def compute_trailing_yoy(actuals):
    """Compute trailing 4-quarter average YoY growth rate for display context."""
    lookup = {}
    for a in actuals:
        d = datetime.strptime(a["period"], "%Y-%m-%d")
        lookup[(d.year, quarter_from_date(a["period"]))] = a["revenue"]
    recent = actuals[-4:]
    yoy_rates = []
    for a in recent:
        d = datetime.strptime(a["period"], "%Y-%m-%d")
        q = quarter_from_date(a["period"])
        prior = lookup.get((d.year - 1, q))
        if prior and prior > 0:
            yoy_rates.append(a["revenue"] / prior - 1)
    return round(statistics.mean(yoy_rates) * 100, 2) if yoy_rates else 0


def extrapolate(actuals, qoq_data, n=4):
    """Project forward n quarters using Kinetic $ QoQ methodology."""
    by_q = defaultdict(list)
    for row in qoq_data:
        by_q[row["quarter"]].append(row["qoq_dollar_change"])

    seasonal_forecasts = {}
    for q in ["Q1", "Q2", "Q3", "Q4"]:
        values = by_q.get(q, [])
        if values:
            trend, projected = classify_seasonal_trend(values)
            seasonal_forecasts[q] = {
                "trend": trend,
                "projected_qoq": projected,
                "history": values
            }

    momentum_label, momentum_factor, recent_qoq = compute_momentum(qoq_data)
    qoq_yoy = compute_qoq_yoy(qoq_data)

    last_d = datetime.strptime(actuals[-1]["period"], "%Y-%m-%d")
    last_q = quarter_from_date(actuals[-1]["period"])
    prev_rev = actuals[-1]["revenue"]

    projections = []
    for i in range(1, n + 1):
        proj_q = ((last_q - 1 + i) % 4) + 1
        proj_year = last_d.year + ((last_q - 1 + i) // 4)
        q_key = f"Q{proj_q}"

        if q_key in seasonal_forecasts:
            base_qoq = seasonal_forecasts[q_key]["projected_qoq"]
            trend = seasonal_forecasts[q_key]["trend"]
        else:
            base_qoq = 0
            trend = "no_data"

        adjusted_qoq = round(base_qoq * momentum_factor)
        proj_rev = prev_rev + adjusted_qoq

        projections.append({
            "period": quarter_end_date(proj_year, proj_q),
            "quarter": q_key,
            "projected_revenue": proj_rev,
            "projected_qoq": adjusted_qoq,
            "seasonal_trend": trend,
            "momentum": momentum_label
        })
        prev_rev = proj_rev

    return projections, seasonal_forecasts, momentum_label, momentum_factor, qoq_yoy


def consensus_comparison(actuals, projections, estimates):
    today = datetime.now()
    current_fy = today.year
    next_fy = current_fy + 1

    next_q_comp = None
    if projections:
        p = projections[0]
        p_date = datetime.strptime(p["period"], "%Y-%m-%d")
        for e in estimates:
            e_date = datetime.strptime(e["period"], "%Y-%m-%d")
            if abs((e_date - p_date).days) < 45:
                diff = p["projected_revenue"] - e["estimated_revenue"]
                pct = (diff / e["estimated_revenue"] * 100) if e["estimated_revenue"] else 0
                next_q_comp = {
                    "period": p["period"],
                    "projection": p["projected_revenue"],
                    "consensus": e["estimated_revenue"],
                    "diff_dollars": diff,
                    "diff_pct": round(pct, 2),
                    "signal": "BEAT" if diff > 0 else "MISS",
                }
                break

    def fy_totals(year):
        act_total = sum(a["revenue"] for a in actuals if datetime.strptime(a["period"], "%Y-%m-%d").year == year)
        act_count = sum(1 for a in actuals if datetime.strptime(a["period"], "%Y-%m-%d").year == year)
        proj_total = sum(p["projected_revenue"] for p in projections if datetime.strptime(p["period"], "%Y-%m-%d").year == year)
        proj_count = sum(1 for p in projections if datetime.strptime(p["period"], "%Y-%m-%d").year == year)
        our_total = act_total + proj_total
        est_total = sum(e["estimated_revenue"] for e in estimates if datetime.strptime(e["period"], "%Y-%m-%d").year == year)
        if est_total == 0:
            return None
        diff = our_total - est_total
        pct = (diff / est_total * 100)
        return {
            "fiscal_year": year,
            "our_total": our_total,
            "consensus_total": est_total,
            "diff_dollars": diff,
            "diff_pct": round(pct, 2),
            "signal": "BEAT" if diff > 0 else "MISS",
            "actual_quarters": act_count,
            "projected_quarters": proj_count,
        }

    return next_q_comp, fy_totals(current_fy), fy_totals(next_fy)


def compute_beat_cadence(ticker):
    """Compute historical beat pattern from FMP earnings endpoint (actual vs pre-earnings estimate)."""
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

    divergence = abs(avg_4 - avg_8)
    is_changing = divergence > 1.5

    if std_4 <= std_8:
        selected = avg_4
        selected_window = "4Q"
    else:
        selected = avg_8
        selected_window = "8Q"

    return {
        "avg_beat_4q": round(avg_4, 2),
        "avg_beat_8q": round(avg_8, 2),
        "std_4q": round(std_4, 2) if std_4 != float("inf") else 0,
        "std_8q": round(std_8, 2) if std_8 != float("inf") else 0,
        "selected_beat_pct": round(selected, 2),
        "selected_window": selected_window,
        "is_changing": is_changing
    }


def build_guide_inference(actuals, estimates, seasonal_forecasts, momentum_factor, beat_cadence):
    """Build 4-quarter guide inference: beat-adjusted actuals + implied guides."""
    if not beat_cadence:
        return None

    beat_pct = beat_cadence["selected_beat_pct"] / 100

    last_date = datetime.strptime(actuals[-1]["period"], "%Y-%m-%d")
    actual_periods = {a["period"] for a in actuals}

    next_q_est = None
    next_q_date = None
    for e in sorted(estimates, key=lambda x: x["period"]):
        e_date = datetime.strptime(e["period"], "%Y-%m-%d")
        if e_date > last_date and e["period"] not in actual_periods and e["estimated_revenue"]:
            next_q_est = e
            next_q_date = e_date
            break

    if not next_q_est:
        return None

    beat_adjusted = round(next_q_est["estimated_revenue"] * (1 + beat_pct))

    start_q = quarter_from_date(next_q_est["period"])
    start_year = next_q_date.year

    prev_rev = None
    quarters = []

    for i in range(4):
        proj_q = ((start_q - 1 + i) % 4) + 1
        proj_year = start_year + ((start_q - 1 + i) // 4)
        period = quarter_end_date(proj_year, proj_q)
        q_key = f"Q{proj_q}"

        if i == 0:
            proj_rev = beat_adjusted
        else:
            if q_key in seasonal_forecasts:
                base_qoq = seasonal_forecasts[q_key]["projected_qoq"]
            else:
                base_qoq = 0
            adjusted_qoq = round(base_qoq * momentum_factor)
            proj_rev = prev_rev + adjusted_qoq

        implied_guide = round(proj_rev / (1 + beat_pct))

        consensus = None
        p_date = datetime.strptime(period, "%Y-%m-%d")
        for e in estimates:
            e_date = datetime.strptime(e["period"], "%Y-%m-%d")
            if abs((e_date - p_date).days) < 45 and e["period"] not in actual_periods:
                if e["estimated_revenue"] and e["estimated_revenue"] != 0:
                    consensus = e["estimated_revenue"]
                    break

        gap_dollars = None
        gap_pct = None
        guide_signal = None
        if consensus:
            gap_dollars = round(implied_guide - consensus)
            gap_pct = round((implied_guide - consensus) / consensus * 100, 2)
            if gap_pct > 2:
                guide_signal = "GUIDE ABOVE"
            elif gap_pct < -2:
                guide_signal = "GUIDE BELOW"
            else:
                guide_signal = "GUIDE IN-LINE"

        quarters.append({
            "period": period,
            "quarter": q_key,
            "projected_actual": proj_rev,
            "implied_guide": implied_guide,
            "consensus": consensus,
            "gap_dollars": gap_dollars,
            "gap_pct": gap_pct,
            "signal": guide_signal
        })
        prev_rev = proj_rev

    return {
        "beat_cadence": beat_cadence,
        "beat_adjusted_quarter": {
            "period": next_q_est["period"],
            "consensus": next_q_est["estimated_revenue"],
            "beat_adjusted": beat_adjusted,
            "expected_beat_pct": beat_cadence["selected_beat_pct"]
        },
        "quarters": quarters
    }


def build_data():
    all_data = {}
    for ticker in TICKERS:
        print(f"Analyzing {ticker}...")
        actuals, estimates = get_db_data(ticker)
        if not actuals:
            continue
        qoq = compute_qoq(actuals)
        seasonal = compute_seasonality(qoq)
        anomalies = flag_anomalies(qoq, seasonal)
        projections, seasonal_forecasts, momentum_label, momentum_factor, qoq_yoy = extrapolate(actuals, qoq, n=4)
        next_q, current_fy, next_fy = consensus_comparison(actuals, projections, estimates)
        beat_cadence = compute_beat_cadence(ticker)
        guide_inference = build_guide_inference(actuals, estimates, seasonal_forecasts, momentum_factor, beat_cadence)

        all_data[ticker] = {
            "actuals": actuals,
            "estimates": estimates,
            "qoq": qoq,
            "seasonal": seasonal,
            "anomalies": anomalies,
            "projections": projections,
            "avg_yoy": compute_trailing_yoy(actuals),
            "seasonal_forecasts": {q: {"trend": v["trend"], "projected_qoq": v["projected_qoq"]} for q, v in seasonal_forecasts.items()},
            "momentum": {"label": momentum_label, "factor": momentum_factor},
            "qoq_yoy": qoq_yoy,
            "guide_inference": guide_inference,
            "consensus": {
                "next_quarter": next_q,
                "current_fy": current_fy,
                "next_fy": next_fy,
            },
        }
    return all_data


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Revenue Analysis Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg-body:#090b10;--bg-header:#0d1017;--bg-card:#12161e;--bg-card-alt:#161b25;
  --bg-table-head:#171c27;--bg-hover:rgba(88,166,255,.04);
  --border:#1b2231;--border-light:#222d3f;
  --text:#e2e8f0;--text-sec:#7b8ba3;--text-muted:#505d72;
  --accent:#58a6ff;--green:#34d399;--red:#f87171;--orange:#fb923c;--yellow:#fbbf24;
  --font-sans:'Inter',-apple-system,'Segoe UI',system-ui,sans-serif;
  --font-mono:'JetBrains Mono','SF Mono','Cascadia Code',Consolas,monospace;
}
html{font-size:14px}
body{background:var(--bg-body);color:var(--text);font-family:var(--font-sans);-webkit-font-smoothing:antialiased}
::selection{background:rgba(88,166,255,.25)}

/* ---- HEADER ---- */
.header{background:var(--bg-header);border-bottom:1px solid var(--border);padding:14px 40px;display:flex;justify-content:space-between;align-items:center}
.header h1{font-size:.8rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--text-sec)}
.header h1 span{color:var(--accent)}
.header .ts{font-family:var(--font-mono);font-size:.72rem;color:var(--text-muted)}

/* ---- TABS ---- */
.tabs{display:flex;gap:1px;padding:0 40px;background:var(--bg-header);border-bottom:1px solid var(--border)}
.tab{padding:11px 28px;background:none;border:none;color:var(--text-muted);font-family:var(--font-mono);font-size:.78rem;font-weight:600;letter-spacing:.6px;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}
.tab:hover{color:var(--text-sec);background:var(--bg-hover)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}

/* ---- LAYOUT ---- */
.wrap{max-width:1440px;margin:0 auto;padding:28px 40px 60px}

/* ---- SIGNAL CARDS ---- */
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:26px}
.card{background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:18px 20px}
.card .lbl{font-size:.65rem;text-transform:uppercase;letter-spacing:.7px;color:var(--text-muted);margin-bottom:8px;font-weight:600}
.card .val{font-family:var(--font-mono);font-size:1.35rem;font-weight:800}
.card .sub{font-family:var(--font-mono);font-size:.68rem;color:var(--text-muted);margin-top:5px}
.card.beat .val{color:var(--green)}.card.beat{border-color:rgba(52,211,153,.15)}
.card.miss .val{color:var(--red)}.card.miss{border-color:rgba(248,113,113,.15)}
.card.neutral .val{color:var(--text)}

/* ---- CHART ---- */
.chart-wrap{background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:22px 24px 16px;margin-bottom:26px;position:relative}
.chart-wrap .chart-title{font-size:.65rem;text-transform:uppercase;letter-spacing:.7px;color:var(--text-muted);font-weight:600;margin-bottom:14px}
.chart-box{height:320px}

/* ---- SEASONALITY ---- */
.season-row{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:26px}
.sqr{background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:16px;text-align:center}
.sqr .ql{font-size:.78rem;font-weight:700;color:var(--accent);margin-bottom:5px}
.sqr .qv{font-family:var(--font-mono);font-size:1.1rem;font-weight:800}
.sqr .qs{font-family:var(--font-mono);font-size:.62rem;color:var(--text-muted);margin-top:3px}
.pos{color:var(--green)}.neg{color:var(--red)}

/* ---- SECTION HEADING ---- */
.sec{font-size:.65rem;text-transform:uppercase;letter-spacing:.7px;color:var(--text-muted);font-weight:600;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border)}

/* ---- TABLES ---- */
.tbl-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:24px}
@media(max-width:1100px){.tbl-grid{grid-template-columns:1fr}}
.tbl-box{background:var(--bg-card);border:1px solid var(--border);border-radius:10px;overflow:hidden}
table{width:100%;border-collapse:collapse}
th{background:var(--bg-table-head);padding:9px 16px;text-align:right;font-size:.62rem;text-transform:uppercase;letter-spacing:.4px;color:var(--text-muted);font-weight:700;border-bottom:1px solid var(--border)}
th:first-child,td:first-child{text-align:left}
td{padding:7px 16px;text-align:right;font-family:var(--font-mono);font-size:.74rem;border-bottom:1px solid rgba(27,34,49,.45);color:var(--text-sec)}
tr:hover{background:var(--bg-hover)}
tr.anom{background:rgba(251,191,36,.04)}
tr.anom td:first-child{box-shadow:inset 3px 0 0 var(--yellow)}
.hi{font-weight:700}
.beat-dot::after{content:" \25CF";font-size:.55rem;vertical-align:middle;margin-left:3px}

/* ---- GUIDE INFERENCE ---- */
.guide-wrap{background:var(--bg-card);border:2px solid var(--accent);border-radius:10px;margin-bottom:26px;overflow:hidden}
.guide-header{padding:18px 24px 14px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px}
.guide-title{font-size:.85rem;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;color:var(--accent)}
.guide-meta{font-family:var(--font-mono);font-size:.72rem;color:var(--text-muted)}
.guide-tbl{width:100%;border-collapse:collapse}
.guide-tbl th{background:var(--bg-table-head);padding:11px 16px;text-align:right;font-size:.65rem;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted);font-weight:700;border-bottom:1px solid var(--border)}
.guide-tbl th:first-child,.guide-tbl td:first-child{text-align:left}
.guide-tbl td{padding:11px 16px;text-align:right;font-family:var(--font-mono);font-size:.82rem;border-bottom:1px solid rgba(27,34,49,.45);color:var(--text-sec)}
.guide-tbl tr:hover{background:var(--bg-hover)}
.guide-above{background:rgba(52,211,153,.06)}.guide-above .signal-cell{color:var(--green);font-weight:800}
.guide-inline{background:rgba(251,191,36,.05)}.guide-inline .signal-cell{color:var(--yellow);font-weight:800}
.guide-below{background:rgba(248,113,113,.06)}.guide-below .signal-cell{color:var(--red);font-weight:800}
.signal-cell{font-size:.7rem;letter-spacing:.3px}
</style>
</head>
<body>

<div class="header">
  <h1><span>Revenue</span> Analysis Dashboard</h1>
  <div class="ts">__TIMESTAMP__</div>
</div>
<nav class="tabs" id="tabs"></nav>
<div class="wrap" id="wrap"></div>

<script>
const DATA = __DATA__;
const TICKERS = Object.keys(DATA);
let chart = null;

/* ---- FORMATTERS ---- */
function fmtRev(v){
  if(v==null)return'N/A';var a=Math.abs(v);
  if(a>=1e9)return'$'+(a/1e9).toFixed(1)+'B';
  if(a>=1e6)return'$'+(a/1e6).toFixed(1)+'M';
  if(a>=1e3)return'$'+(a/1e3).toFixed(0)+'K';
  return'$'+a.toFixed(0);
}
function fmtDelta(v){
  if(v==null)return'N/A';
  return(v>=0?'+':'\u2212')+fmtRev(v);
}
function fmtPct(v){
  if(v==null)return'N/A';
  return(v>0?'+':'')+v.toFixed(1)+'%';
}
function fmtDate(s){
  var d=new Date(s+'T00:00:00');
  return d.toLocaleDateString('en-US',{month:'short',year:'numeric'});
}
function cls(v){return v>0?'pos':v<0?'neg':''}
function sigCls(s){return s==='BEAT'?'beat':s==='MISS'?'miss':'neutral'}

/* ---- INIT ---- */
document.addEventListener('DOMContentLoaded',function(){
  var nav=document.getElementById('tabs');
  TICKERS.forEach(function(t){
    var b=document.createElement('button');b.className='tab';b.textContent=t;
    b.onclick=function(){select(t)};nav.appendChild(b);
  });
  if(TICKERS.length)select(TICKERS[0]);
});

function select(ticker){
  document.querySelectorAll('.tab').forEach(function(t){t.classList.toggle('active',t.textContent===ticker)});
  render(ticker);
}

/* ---- RENDER ---- */
function render(ticker){
  var d=DATA[ticker],w=document.getElementById('wrap');
  w.innerHTML=buildCards(d)+
    buildGuideTable(d)+
    '<div class="chart-wrap"><div class="chart-title">Quarterly Revenue &mdash; Actuals / Projection / Consensus</div><div class="chart-box"><canvas id="cv"></canvas></div></div>'+
    buildSeason(d)+
    '<div class="tbl-grid"><div>'+buildHistTable(d)+'</div><div>'+buildProjTable(d)+'</div></div>';
  buildChart(d);
}

/* ---- SIGNAL CARDS ---- */
function buildCards(d){
  var c=d.consensus,h='<div class="cards">';
  var ml=d.momentum?d.momentum.label:'N/A';var mc=ml==='accelerating'?'beat':ml==='decelerating'?'miss':'neutral';
  h+='<div class="card '+mc+'"><div class="lbl">Momentum</div><div class="val">'+ml.toUpperCase()+'</div><div class="sub">YoY: '+fmtPct(d.avg_yoy)+' trailing</div></div>';
  if(c.next_quarter){var q=c.next_quarter;h+='<div class="card '+sigCls(q.signal)+'"><div class="lbl">Next Quarter</div><div class="val">'+q.signal+' '+fmtPct(q.diff_pct)+'</div><div class="sub">'+fmtRev(q.projection)+' vs '+fmtRev(q.consensus)+' est</div></div>';}
  else h+='<div class="card neutral"><div class="lbl">Next Quarter</div><div class="val">N/A</div><div class="sub">No consensus match</div></div>';
  if(c.current_fy){var f=c.current_fy;h+='<div class="card '+sigCls(f.signal)+'"><div class="lbl">FY '+f.fiscal_year+'</div><div class="val">'+f.signal+' '+fmtPct(f.diff_pct)+'</div><div class="sub">'+fmtRev(f.our_total)+' vs '+fmtRev(f.consensus_total)+'</div></div>';}
  else h+='<div class="card neutral"><div class="lbl">Current FY</div><div class="val">N/A</div><div class="sub">No consensus data</div></div>';
  if(c.next_fy){var n=c.next_fy;h+='<div class="card '+sigCls(n.signal)+'"><div class="lbl">FY '+n.fiscal_year+'</div><div class="val">'+n.signal+' '+fmtPct(n.diff_pct)+'</div><div class="sub">'+fmtRev(n.our_total)+' vs '+fmtRev(n.consensus_total)+'</div></div>';}
  else h+='<div class="card neutral"><div class="lbl">Next FY</div><div class="val">N/A</div><div class="sub">No consensus data</div></div>';
  return h+'</div>';
}

/* ---- GUIDE INFERENCE ---- */
function buildGuideTable(d){
  var gi=d.guide_inference;
  if(!gi)return '<div class="guide-wrap"><div class="guide-header"><div class="guide-title">Implied Guide Inference</div></div><div style="color:var(--text-muted);font-size:.8rem;padding:20px;font-family:var(--font-mono)">Insufficient beat history for guide inference</div></div>';
  var bc=gi.beat_cadence,bq=gi.beat_adjusted_quarter;
  var h='<div class="guide-wrap"><div class="guide-header"><div class="guide-title">Implied Guide Inference</div>';
  h+='<div class="guide-meta">Beat Cadence: <span class="'+cls(bc.selected_beat_pct)+'">'+fmtPct(bc.selected_beat_pct)+'</span> ('+bc.selected_window+')';
  if(bc.is_changing)h+=' &bull; <span style="color:var(--orange)">PATTERN CHANGING</span>';
  h+=' &bull; Next Q Implied Actual: '+fmtRev(bq.beat_adjusted)+' vs '+fmtRev(bq.consensus)+' est</div></div>';
  h+='<table class="guide-tbl"><thead><tr><th>Period</th><th>Q</th><th>Projected Actual</th><th>Implied Guide</th><th>Consensus</th><th>Gap $</th><th>Gap %</th><th>Signal</th></tr></thead><tbody>';
  gi.quarters.forEach(function(q){
    var sc=q.signal==='GUIDE ABOVE'?'guide-above':q.signal==='GUIDE BELOW'?'guide-below':q.signal==='GUIDE IN-LINE'?'guide-inline':'';
    h+='<tr class="'+sc+'">';
    h+='<td>'+fmtDate(q.period)+'</td><td>'+q.quarter+'</td>';
    h+='<td class="hi">'+fmtRev(q.projected_actual)+'</td>';
    h+='<td class="hi">'+fmtRev(q.implied_guide)+'</td>';
    h+='<td>'+(q.consensus!=null?fmtRev(q.consensus):'N/A')+'</td>';
    h+='<td class="'+cls(q.gap_dollars)+'">'+(q.gap_dollars!=null?fmtDelta(q.gap_dollars):'N/A')+'</td>';
    h+='<td class="'+cls(q.gap_pct)+' hi">'+(q.gap_pct!=null?fmtPct(q.gap_pct):'N/A')+'</td>';
    h+='<td class="signal-cell">'+(q.signal||'N/A')+'</td></tr>';
  });
  return h+'</tbody></table></div>';
}

/* ---- SEASONALITY ---- */
function buildSeason(d){
  var s=d.seasonal,sf=d.seasonal_forecasts||{};
  var h='<div class="sec">Seasonal $ QoQ &mdash; Trend &amp; Projected</div><div class="season-row">';
  ['Q1','Q2','Q3','Q4'].forEach(function(q){
    var f=sf[q],v=s[q];
    if(f){h+='<div class="sqr"><div class="ql">'+q+' <span style="font-weight:400;color:var(--text-muted);font-size:.6rem">'+f.trend+'</span></div><div class="qv '+cls(f.projected_qoq)+'">'+fmtDelta(f.projected_qoq)+'</div><div class="qs">projected $ QoQ</div></div>';}
    else if(v){h+='<div class="sqr"><div class="ql">'+q+'</div><div class="qv '+cls(v.avg_qoq_change)+'">'+fmtDelta(v.avg_qoq_change)+'</div><div class="qs">\u00B1'+fmtRev(Math.abs(v.std_qoq_change))+' &middot; '+v.observations+' obs</div></div>';}
    else h+='<div class="sqr"><div class="ql">'+q+'</div><div class="qv">N/A</div></div>';
  });
  return h+'</div>';
}

/* ---- HISTORICAL TABLE ---- */
function buildHistTable(d){
  var rows=d.qoq.slice(-12),an=d.anomalies||[];
  var h='<div class="sec">Historical Quarterly Revenue (Last 12Q)</div><div class="tbl-box"><table><thead><tr><th>Period</th><th>Q</th><th>Revenue</th><th>QoQ $</th><th>QoQ %</th></tr></thead><tbody>';
  rows.forEach(function(r){
    var a=an.indexOf(r.period)!==-1;
    h+='<tr'+(a?' class="anom"':'')+'>';
    h+='<td>'+fmtDate(r.period)+'</td><td>'+r.quarter+'</td><td class="hi">'+fmtRev(r.revenue)+'</td>';
    h+='<td class="'+cls(r.qoq_dollar_change)+'">'+fmtDelta(r.qoq_dollar_change)+'</td>';
    h+='<td class="'+cls(r.qoq_pct_change)+'">'+fmtPct(r.qoq_pct_change)+'</td></tr>';
  });
  return h+'</tbody></table></div>';
}

/* ---- PROJECTION TABLE ---- */
function buildProjTable(d){
  var proj=d.projections,est=d.estimates;
  var matched=proj.map(function(p){
    var pd=new Date(p.period+'T00:00:00'),best=null,bd=Infinity;
    est.forEach(function(e){
      var ed=new Date(e.period+'T00:00:00'),diff=Math.abs(ed-pd)/(864e5);
      if(diff<45&&diff<bd){bd=diff;best=e;}
    });
    var con=best?best.estimated_revenue:null,div=null;
    if(con&&con!==0)div=(p.projected_revenue-con)/con*100;
    return{period:p.period,quarter:p.quarter,rev:p.projected_revenue,qoq:p.projected_qoq||0,con:con,div:div,trend:p.seasonal_trend||''};
  });
  var h='<div class="sec">4-Quarter Projection vs Consensus ($ QoQ Method)</div><div class="tbl-box"><table><thead><tr><th>Period</th><th>Q</th><th>Projected</th><th>$ QoQ</th><th>Consensus</th><th>Div %</th><th>Signal</th></tr></thead><tbody>';
  matched.forEach(function(r){
    var sig=r.div!=null?(r.div>0?'BEAT':'MISS'):'',sc=cls(r.div);
    var hc=r.div!=null&&Math.abs(r.div)>5;
    h+='<tr><td>'+fmtDate(r.period)+'</td><td>'+r.quarter+'</td>';
    h+='<td class="hi">'+fmtRev(r.rev)+'</td>';
    h+='<td class="'+cls(r.qoq)+'">'+fmtDelta(r.qoq)+'</td>';
    h+='<td>'+(r.con!=null?fmtRev(r.con):'N/A')+'</td>';
    h+='<td class="'+sc+' hi">'+(r.div!=null?fmtPct(r.div):'N/A')+'</td>';
    h+='<td class="'+sc+(hc?' beat-dot':'')+'">'+sig+'</td></tr>';
  });
  return h+'</tbody></table></div>';
}

/* ---- CHART ---- */
function buildChart(d){
  var cv=document.getElementById('cv');if(!cv)return;
  if(chart){chart.destroy();chart=null;}

  var act=d.actuals.slice(-20);
  var proj=d.projections;
  var tStart=act[0].period,tEnd=proj[proj.length-1].period;
  var est=d.estimates.filter(function(e){return e.period>=tStart&&e.period<=tEnd;});

  var dAct=act.map(function(a){return{x:a.period,y:a.revenue}});
  var dProj=[{x:act[act.length-1].period,y:act[act.length-1].revenue}].concat(proj.map(function(p){return{x:p.period,y:p.projected_revenue}}));
  var dEst=est.map(function(e){return{x:e.period,y:e.estimated_revenue}});

  chart=new Chart(cv,{
    type:'line',
    data:{datasets:[
      {label:'Actuals',data:dAct,borderColor:'#e2e8f0',backgroundColor:'rgba(226,232,240,.06)',borderWidth:2,pointRadius:2.5,pointBackgroundColor:'#e2e8f0',tension:.15,fill:true,order:1},
      {label:'Projection',data:dProj,borderColor:'#58a6ff',backgroundColor:'rgba(88,166,255,.06)',borderWidth:2,borderDash:[7,4],pointRadius:3,pointBackgroundColor:'#58a6ff',pointStyle:'rectRot',tension:.15,fill:true,order:2},
      {label:'Consensus',data:dEst,borderColor:'#fb923c',backgroundColor:'transparent',borderWidth:1.5,borderDash:[4,4],pointRadius:2,pointBackgroundColor:'#fb923c',tension:.15,fill:false,order:3}
    ]},
    options:{
      responsive:true,maintainAspectRatio:false,
      interaction:{intersect:false,mode:'index'},
      plugins:{
        legend:{position:'top',align:'end',labels:{color:'#7b8ba3',font:{family:"'JetBrains Mono',monospace",size:10.5},usePointStyle:true,pointStyleWidth:16,padding:18}},
        tooltip:{backgroundColor:'#171c27',titleColor:'#e2e8f0',bodyColor:'#7b8ba3',borderColor:'#1b2231',borderWidth:1,padding:10,
          titleFont:{family:"'JetBrains Mono',monospace",size:11},bodyFont:{family:"'JetBrains Mono',monospace",size:11},
          callbacks:{label:function(c){return' '+c.dataset.label+': '+fmtRev(c.parsed.y)}}}
      },
      scales:{
        x:{type:'time',time:{unit:'quarter',displayFormats:{quarter:'MMM yy'}},grid:{color:'rgba(27,34,49,.6)',lineWidth:.5},ticks:{color:'#505d72',font:{family:"'JetBrains Mono',monospace",size:9.5},maxRotation:45},border:{color:'var(--border)'}},
        y:{grid:{color:'rgba(27,34,49,.6)',lineWidth:.5},ticks:{color:'#505d72',font:{family:"'JetBrains Mono',monospace",size:9.5},callback:function(v){return fmtRev(v)}},border:{color:'var(--border)'}}
      }
    }
  });
}
</script>
</body>
</html>"""


def generate_html(data):
    ts = datetime.now().strftime("%B %d, %Y  %H:%M")
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(data))
    html = html.replace("__TIMESTAMP__", ts)
    return html


def main():
    parser = argparse.ArgumentParser(description="Generate Kinetic revenue dashboard")
    add_credentials_args(parser)
    args = parser.parse_args()
    load_credentials(secret_name=args.secrets, region=args.region)

    data = build_data()
    html = generate_html(data)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Dashboard written to {path}")
    webbrowser.open(path)


if __name__ == "__main__":
    main()
