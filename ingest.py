import requests
import psycopg2
import time
from dotenv import load_dotenv
import os
from datetime import datetime

load_dotenv()

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

# Fiscal year-end month for non-calendar-year companies.
# Default is 12 (calendar year). To add a new company with an unusual
# fiscal calendar, add one line here — everything else adapts automatically.
FISCAL_CALENDAR = {
    "SNOW": 1, "MDB": 1, "WDAY": 1, "VEEV": 1,
    "ADSK": 1, "OKTA": 1, "CRWD": 1, "GTLB": 1,
    "S": 1, "NCNO": 1, "BRZE": 1, "KVYO": 1,
    "ADBE": 11,
    "INTU": 7,
    "TEAM": 6,
    "PANW": 7,
    "SNPS": 10,
    "ZS": 7,
    "BILL": 6,
    "ESTC": 4,
    "DT": 3,
    "PCTY": 6,
    "GWRE": 7,
    "AZPN": 6,
}


# Manual consensus overrides — applied after every ingest run.
# Use this for corrections where FMP data is wrong or missing.
# Key: (ticker, period), Value: estimated_revenue in USD
CONSENSUS_OVERRIDES = {
    ("DDOG", "2026-03-31"): 956000000,
}


def apply_consensus_overrides():
    """Apply manual consensus overrides independently of a full ingest run.
    Can be called from export.py or any other module to ensure overrides
    are always in the DB before generating output."""
    if not CONSENSUS_OVERRIDES:
        return
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"), database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"),
        port=5432, sslmode="require")
    cur = conn.cursor()
    for (tk, period), rev in CONSENSUS_OVERRIDES.items():
        cur.execute("""
            INSERT INTO consensus_estimates (ticker, period, estimated_revenue)
            VALUES (%s, %s, %s)
            ON CONFLICT (ticker, period) DO UPDATE SET estimated_revenue = EXCLUDED.estimated_revenue
        """, (tk, period, rev))
    conn.commit()
    cur.close()
    conn.close()
    print(f"[overrides] Applied {len(CONSENSUS_OVERRIDES)} consensus override(s)")


def fetch_and_store():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        port=5432,
        sslmode="require"
    )
    cur = conn.cursor()

    for ticker in TICKERS:
        # Pull historical revenue actuals (quarterly)
        url = f"https://financialmodelingprep.com/stable/income-statement?symbol={ticker}&period=quarter&limit=40&apikey={os.getenv('FMP_API_KEY')}"
        data = requests.get(url).json()
        time.sleep(0.5)

        if isinstance(data, list):
            for q in data:
                cur.execute("""
                    INSERT INTO revenue_actuals (ticker, period, revenue)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (ticker, period) DO UPDATE SET revenue = EXCLUDED.revenue
                """, (ticker, q["date"], q["revenue"]))
        else:
            print(f"Unexpected actuals response for {ticker}: {data}")

        # Fix Q4 actuals: FMP stores fiscal year cumulative in Q4 row
        fix_q4(ticker, cur)

        # Rebuild consensus estimates: clear stale data, insert quarterly,
        # then distribute historical annual estimates into quarterly
        rebuild_consensus(ticker, cur)

        # Store pre-earnings consensus from FMP /earnings endpoint
        ingest_pre_earnings_consensus(ticker, cur)

        print(f"Done: {ticker}")

    # Apply manual consensus overrides
    if CONSENSUS_OVERRIDES:
        for (tk, period), rev in CONSENSUS_OVERRIDES.items():
            cur.execute("""
                INSERT INTO consensus_estimates (ticker, period, estimated_revenue)
                VALUES (%s, %s, %s)
                ON CONFLICT (ticker, period) DO UPDATE SET estimated_revenue = EXCLUDED.estimated_revenue
            """, (tk, period, rev))
        print(f"Applied {len(CONSENSUS_OVERRIDES)} consensus override(s)")

    conn.commit()
    cur.close()
    conn.close()
    print("All data loaded successfully")


def fix_q4(ticker, cur):
    """Q4 from FMP stores the full fiscal year cumulative revenue.
    Correct it: Q4_standalone = annual_revenue - (Q1 + Q2 + Q3)."""

    url = f"https://financialmodelingprep.com/stable/income-statement?symbol={ticker}&period=annual&limit=12&apikey={os.getenv('FMP_API_KEY')}"
    annual_data = requests.get(url).json()
    time.sleep(0.5)

    if not isinstance(annual_data, list):
        return

    cur.execute(
        "SELECT period, revenue FROM revenue_actuals WHERE ticker=%s ORDER BY period ASC",
        (ticker,)
    )
    quarters = {str(r[0]): r[1] for r in cur.fetchall()}

    for yr in annual_data:
        annual_date = yr["date"]
        annual_rev = yr["revenue"]
        annual_dt = datetime.strptime(annual_date, "%Y-%m-%d")

        q4_period = None
        q4_dist = float("inf")
        for qp in quarters:
            dist = abs((datetime.strptime(qp, "%Y-%m-%d") - annual_dt).days)
            if dist < q4_dist:
                q4_dist = dist
                q4_period = qp

        if q4_period is None or q4_dist > 45:
            continue

        q4_dt = datetime.strptime(q4_period, "%Y-%m-%d")
        preceding = []
        for qp, rev in quarters.items():
            days_before = (q4_dt - datetime.strptime(qp, "%Y-%m-%d")).days
            if 30 < days_before < 400:
                preceding.append((days_before, rev))

        preceding.sort(key=lambda x: x[0])
        q123 = preceding[:3]

        if len(q123) < 3:
            continue

        q123_sum = sum(r for _, r in q123)
        q4_standalone = annual_rev - q123_sum

        if q4_standalone > 0 and q4_standalone != quarters[q4_period]:
            cur.execute(
                "UPDATE revenue_actuals SET revenue = %s WHERE ticker = %s AND period = %s",
                (q4_standalone, ticker, q4_period)
            )
            print(f"  Fixed {ticker} Q4 actual {q4_period}: {quarters[q4_period]:,} -> {q4_standalone:,}")
            quarters[q4_period] = q4_standalone


def _prev_quarter_date(date_str):
    """Step back 3 months from a period date, returning last day of that month."""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    m = d.month - 3
    y = d.year
    if m <= 0:
        m += 12
        y -= 1
    import calendar as cal
    last_day = cal.monthrange(y, m)[1]
    return f"{y}-{m:02d}-{last_day:02d}"


def _fill_missing_quarterly_from_annual(ticker, cur, quarterly_periods):
    """Derive missing forward quarterly estimates from FMP annual estimates.

    For each annual estimate, compute 4 fiscal quarter periods (Q4 = annual date,
    step back 3 months for Q3/Q2/Q1). If any quarter is missing from quarterly_periods,
    distribute the annual estimate using the most recent complete FY's actual
    seasonal revenue proportions.
    """
    # Fetch annual estimates from FMP
    url = f"https://financialmodelingprep.com/stable/analyst-estimates?symbol={ticker}&period=annual&apikey={os.getenv('FMP_API_KEY')}"
    annual_est = requests.get(url).json()
    time.sleep(0.5)

    if not isinstance(annual_est, list) or not annual_est:
        return

    # Get all actuals to compute seasonal proportions
    cur.execute(
        "SELECT period, revenue FROM revenue_actuals WHERE ticker=%s ORDER BY period ASC",
        (ticker,)
    )
    all_actuals = [(str(r[0]), r[1]) for r in cur.fetchall()]
    if len(all_actuals) < 4:
        return

    # Compute proportions from the most recent 4 quarters (last complete FY)
    last_4 = all_actuals[-4:]
    fy_total = sum(rev for _, rev in last_4)
    if fy_total <= 0:
        return
    proportions = [rev / fy_total for _, rev in last_4]
    # proportions[0] = Q1 proportion, [1] = Q2, [2] = Q3, [3] = Q4

    actual_periods = {per for per, _ in all_actuals}
    derived = 0

    for ae in annual_est:
        annual_date = ae.get("date", "")
        annual_rev = ae.get("revenueAvg", 0)
        if not annual_date or not annual_rev or annual_rev <= 0:
            continue

        # Compute 4 fiscal quarter periods: Q4 = annual_date, step back for Q3/Q2/Q1
        q4 = annual_date
        q3 = _prev_quarter_date(q4)
        q2 = _prev_quarter_date(q3)
        q1 = _prev_quarter_date(q2)
        fy_quarters = [q1, q2, q3, q4]

        for i, per in enumerate(fy_quarters):
            if per in quarterly_periods:
                continue  # already have a quarterly estimate
            if per in actual_periods:
                continue  # already reported — backfill step will handle it

            # Derive quarterly estimate using seasonal proportion
            derived_rev = round(annual_rev * proportions[i])
            cur.execute("""
                INSERT INTO consensus_estimates (ticker, period, estimated_revenue)
                VALUES (%s, %s, %s)
                ON CONFLICT (ticker, period) DO UPDATE SET estimated_revenue = EXCLUDED.estimated_revenue
            """, (ticker, per, derived_rev))
            quarterly_periods.add(per)
            derived += 1

    if derived > 0:
        print(f"  Derived {ticker} {derived} quarterly estimates from annual forecasts")


def rebuild_consensus(ticker, cur):
    """Rebuild quarterly consensus estimates from scratch.
    1. Clear all existing estimates for the ticker.
    2. Insert true quarterly estimates from FMP quarterly API (future periods).
    3. Fetch annual estimates and distribute into quarterly for historical periods
       using actual revenue proportions."""

    # Step 1: Clear stale data
    cur.execute("DELETE FROM consensus_estimates WHERE ticker = %s", (ticker,))

    # Step 2: Insert true quarterly estimates from FMP (future periods only)
    url_q = f"https://financialmodelingprep.com/stable/analyst-estimates?symbol={ticker}&period=quarter&apikey={os.getenv('FMP_API_KEY')}"
    quarterly_est = requests.get(url_q).json()
    time.sleep(0.5)

    quarterly_periods = set()
    if isinstance(quarterly_est, list):
        for e in quarterly_est:
            cur.execute("""
                INSERT INTO consensus_estimates (ticker, period, estimated_revenue)
                VALUES (%s, %s, %s)
                ON CONFLICT (ticker, period) DO UPDATE SET estimated_revenue = EXCLUDED.estimated_revenue
            """, (ticker, e["date"], e.get("revenueAvg", 0)))
            quarterly_periods.add(e["date"])

    # Step 3: Distribute annual estimates into missing forward quarterly slots
    _fill_missing_quarterly_from_annual(ticker, cur, quarterly_periods)

    # Step 4: For historical periods (where actuals exist), set consensus = actuals
    cur.execute(
        "SELECT period, revenue FROM revenue_actuals WHERE ticker=%s ORDER BY period ASC",
        (ticker,)
    )
    actuals = cur.fetchall()

    inserted = 0
    for period, revenue in actuals:
        period_str = str(period)
        if period_str not in quarterly_periods:
            cur.execute("""
                INSERT INTO consensus_estimates (ticker, period, estimated_revenue)
                VALUES (%s, %s, %s)
                ON CONFLICT (ticker, period) DO UPDATE SET estimated_revenue = EXCLUDED.estimated_revenue
            """, (ticker, period_str, revenue))
            inserted += 1

    if inserted > 0:
        print(f"  Set {ticker} historical consensus = actuals for {inserted} quarters")


def ingest_pre_earnings_consensus(ticker, cur):
    """Store pre-earnings consensus from FMP /earnings endpoint.

    Maps each earnings announcement date to the fiscal quarter-end period
    by finding the closest revenue_actuals period before the announcement.
    Only stores reported quarters (revenueActual is not null).
    """
    url = f"https://financialmodelingprep.com/stable/earnings?symbol={ticker}&apikey={os.getenv('FMP_API_KEY')}"
    data = requests.get(url).json()
    time.sleep(0.5)

    if not isinstance(data, list):
        return

    # Get all actual periods for this ticker to map earnings dates
    cur.execute(
        "SELECT period FROM revenue_actuals WHERE ticker=%s ORDER BY period ASC",
        (ticker,)
    )
    actual_periods = [str(r[0]) for r in cur.fetchall()]

    inserted = 0
    for e in data:
        ra = e.get("revenueActual")
        re = e.get("revenueEstimated")
        if ra is None or re is None or re <= 0:
            continue

        earnings_date = e.get("date", "")
        if not earnings_date:
            continue

        # Find the most recent actual period on or before the earnings date
        period = None
        for p in reversed(actual_periods):
            if p <= earnings_date:
                period = p
                break

        if not period:
            continue

        cur.execute("""
            INSERT INTO pre_earnings_consensus (ticker, period, estimated_revenue)
            VALUES (%s, %s, %s)
            ON CONFLICT (ticker, period) DO UPDATE SET estimated_revenue = EXCLUDED.estimated_revenue
        """, (ticker, period, re))
        inserted += 1

    if inserted > 0:
        print(f"  Stored {ticker} pre-earnings consensus for {inserted} quarters")


if __name__ == "__main__":
    fetch_and_store()
