import psycopg2
import anthropic
import os
import statistics
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
TICKERS = ["SNOW", "DDOG", "MDB", "TENB", "QLYS"]


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        port=5432,
        sslmode="require",
    )


def ensure_analysis_column():
    """Add transcript_analysis column to transcripts table if missing."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS transcript_analysis TEXT"
    )
    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# Anomaly detection — mirrors agent.py logic
# ---------------------------------------------------------------------------

def quarter_from_date(date_str):
    month = datetime.strptime(date_str, "%Y-%m-%d").month
    return (month - 1) // 3 + 1


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
            dev = (
                abs(row["qoq_dollar_change"] - seasonal[q]["avg_qoq_change"])
                / seasonal[q]["std_qoq_change"]
            )
            if dev > 1.5:
                flagged.append({
                    "period": row["period"],
                    "quarter": q,
                    "revenue": row["revenue"],
                    "prev_revenue": row["prev_revenue"],
                    "actual_qoq_change": row["qoq_dollar_change"],
                    "qoq_pct_change": row["qoq_pct_change"],
                    "seasonal_avg": seasonal[q]["avg_qoq_change"],
                    "std_deviations": round(dev, 2),
                })
    return flagged


# ---------------------------------------------------------------------------
# Transcript lookup & Claude analysis
# ---------------------------------------------------------------------------

def get_transcript(ticker, period, cur):
    cur.execute(
        "SELECT transcript FROM transcripts WHERE ticker=%s AND period=%s",
        (ticker, period),
    )
    row = cur.fetchone()
    return row[0] if row else None


def analyze_transcript(ticker, anomaly, transcript_text):
    """Ask Claude to identify what management said that explains the anomaly."""
    direction = (
        "significantly above"
        if anomaly["actual_qoq_change"] > anomaly["seasonal_avg"]
        else "significantly below"
    )

    prompt = f"""You are a senior financial analyst reviewing an earnings call transcript for {ticker}.

This quarter ({anomaly['period']}, {anomaly['quarter']}) has been flagged as anomalous in our quantitative revenue analysis:

- Revenue: ${anomaly['revenue']:,}
- Prior quarter revenue: ${anomaly['prev_revenue']:,}
- QoQ $ change: ${anomaly['actual_qoq_change']:,}
- QoQ % change: {anomaly['qoq_pct_change']}%
- Seasonal average QoQ $ change: ${anomaly['seasonal_avg']:,}
- Standard deviations from seasonal norm: {anomaly['std_deviations']}

The QoQ change was {direction} the seasonal average, indicating a distortion from the normal pattern.

Below is the earnings call transcript for this quarter. Analyze it and identify what management said that could explain the revenue distortion. Focus specifically on:

1. **One-time items** — large deals, catch-up revenue, contract true-ups, deferred revenue recognition
2. **Macro headwinds/tailwinds** — economic conditions, budget freezes, spending acceleration
3. **Product transitions** — new product launches, sunset of old products, migration impacts
4. **Deal push-outs or pull-forwards** — deals delayed or accelerated from adjacent quarters
5. **Pricing changes** — price increases, discount programs, consumption-based pricing shifts
6. **Customer dynamics** — large customer churn, major new logos, expansion/contraction patterns

For each factor you identify, quote the relevant management statement and explain how it connects to the anomalous revenue figure.

Conclude with a 2-3 sentence summary of the most likely explanation and whether the distortion is likely to be one-time or structural.

TRANSCRIPT:
{transcript_text[:50000]}"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_analysis():
    ensure_analysis_column()

    conn = get_db_connection()
    cur = conn.cursor()

    for ticker in TICKERS:
        print(f"\n{'=' * 60}")
        print(f"  {ticker} — Transcript Anomaly Analysis")
        print(f"{'=' * 60}")

        # Load actuals
        cur.execute(
            "SELECT period, revenue FROM revenue_actuals "
            "WHERE ticker=%s ORDER BY period ASC",
            (ticker,),
        )
        actuals = [{"period": str(r[0]), "revenue": r[1]} for r in cur.fetchall()]

        if len(actuals) < 2:
            print(f"  Insufficient data for {ticker}")
            continue

        # Detect anomalies (same 1.5-sigma threshold as agent.py)
        qoq = compute_qoq(actuals)
        seasonal = compute_seasonality(qoq)
        anomalies = flag_anomalies(qoq, seasonal)

        if not anomalies:
            print(f"  No anomalous quarters detected for {ticker}")
            continue

        print(f"  Found {len(anomalies)} anomalous quarters")

        for anomaly in anomalies:
            period = anomaly["period"]
            print(
                f"\n  Analyzing {period} ({anomaly['quarter']}) — "
                f"{anomaly['std_deviations']} sigma deviation..."
            )

            transcript = get_transcript(ticker, period, cur)
            if not transcript:
                print(f"    No transcript available for {period} — skipping")
                continue

            try:
                analysis = analyze_transcript(ticker, anomaly, transcript)
            except Exception as e:
                print(f"    Error analyzing transcript: {e}")
                continue

            cur.execute(
                "UPDATE transcripts SET transcript_analysis = %s "
                "WHERE ticker = %s AND period = %s",
                (analysis, ticker, period),
            )

            print(f"    Analysis stored ({len(analysis):,} chars)")
            print(f"\n{analysis.encode('ascii', errors='replace').decode('ascii')}\n")

    conn.commit()
    cur.close()
    conn.close()
    print("\nAll transcript analyses complete")


if __name__ == "__main__":
    run_analysis()
