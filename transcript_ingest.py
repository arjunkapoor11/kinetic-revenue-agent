import requests
import psycopg2
import re
import os
import time
import calendar
from datetime import datetime
from dotenv import load_dotenv

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

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
RAPIDAPI_HOST = "seeking-alpha.p.rapidapi.com"

HEADERS = {
    "x-rapidapi-key": RAPIDAPI_KEY,
    "x-rapidapi-host": RAPIDAPI_HOST,
}


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        port=5432,
        sslmode="require",
    )


def strip_html(html_text):
    """Remove HTML tags and decode common entities to plain text."""
    text = re.sub(r"<br\s*/?>", "\n", html_text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in [
        ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " "),
        ("&#x27;", "'"), ("&apos;", "'"),
    ]:
        text = text.replace(entity, char)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fiscal_quarter_end(ticker, fy_year, fy_quarter):
    """Return the calendar date a fiscal quarter ends on.

    For a company whose fiscal year ends in month M:
      FQ4 → month M, year fy_year
      FQ3 → month M-3  (may roll into prior calendar year)
      FQ2 → month M-6
      FQ1 → month M-9

    Defaults to calendar quarters (M=12) for tickers not in FISCAL_CALENDAR.
    """
    fy_end_month = FISCAL_CALENDAR.get(ticker, 12)
    month = fy_end_month - (4 - fy_quarter) * 3
    year = fy_year
    if month <= 0:
        month += 12
        year -= 1
    last_day = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-{last_day:02d}"


def parse_quarter_from_title(title):
    """Extract fiscal quarter and year from transcript title.

    Handles 'Q3 2025', 'FQ3 2025', and '2025 Q3' formats.
    """
    match = re.search(r"F?Q(\d)\s+(\d{4})", title)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"(\d{4})\s+F?Q(\d)", title)
    if match:
        return int(match.group(2)), int(match.group(1))
    return None, None


def find_period_for_transcript(ticker, publish_date, cur):
    """Map a transcript publish date to the DB period it covers.

    Earnings calls happen after the quarter ends, so the most recent
    period on or before the publish date is the quarter being discussed.
    """
    cur.execute(
        "SELECT period FROM revenue_actuals WHERE ticker=%s AND period <= %s "
        "ORDER BY period DESC LIMIT 1",
        (ticker, publish_date),
    )
    row = cur.fetchone()
    return str(row[0]) if row else None


def get_transcript_list(ticker):
    """Get the last 4 earnings call transcript articles from Seeking Alpha."""
    url = "https://seeking-alpha.p.rapidapi.com/transcripts/v2/list"
    params = {"id": ticker.lower(), "size": "20", "number": "1"}
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    data = resp.json()

    articles = []
    for item in data.get("data", []):
        attrs = item.get("attributes", {})
        title = attrs.get("title", "")
        if "earnings call transcript" not in title.lower():
            continue
        articles.append({
            "id": item["id"],
            "title": title,
            "publishOn": attrs.get("publishOn", ""),
        })
        if len(articles) >= 2:
            break
    return articles


def get_transcript_content(article_id):
    """Fetch the full transcript body for an article ID."""
    url = "https://seeking-alpha.p.rapidapi.com/transcripts/v2/get-details"
    params = {"id": article_id}
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    data = resp.json()

    content_html = data.get("data", {}).get("attributes", {}).get("content", "")
    return strip_html(content_html)


def resolve_period(ticker, article, cur):
    """Determine the DB period a transcript article corresponds to.

    Only returns periods that actually exist in revenue_actuals.

    Strategy:
      1. Use publish date to find the most recent revenue_actuals period
         before the transcript was published (handles all fiscal calendars).
      2. Fall back to title Q#/year → nearest DB period within 90 days.
    """
    publish_date = article["publishOn"][:10] if article["publishOn"] else None

    # Primary: publish-date lookup — earnings calls happen after quarter close,
    # so the most recent period before publish is the quarter being discussed.
    if publish_date:
        period = find_period_for_transcript(ticker, publish_date, cur)
        if period:
            return period

    # Fallback: parse Q#/year from title, compute fiscal-calendar-aware
    # candidate date, find nearest DB period within 90 days.
    q_num, q_year = parse_quarter_from_title(article["title"])
    if q_num and q_year:
        candidate = fiscal_quarter_end(ticker, q_year, q_num)
        cur.execute(
            "SELECT period FROM revenue_actuals WHERE ticker=%s "
            "AND ABS(period::date - %s::date) < 90 "
            "ORDER BY ABS(period::date - %s::date) LIMIT 1",
            (ticker, candidate, candidate),
        )
        row = cur.fetchone()
        if row:
            return str(row[0])

    return None


def ingest_transcripts():
    conn = get_db_connection()
    cur = conn.cursor()

    for ticker in TICKERS:
        print(f"\nProcessing {ticker}...")

        try:
            articles = get_transcript_list(ticker)
            time.sleep(1)
        except Exception as e:
            print(f"  Error fetching transcript list for {ticker}: {e}")
            continue

        print(f"  Found {len(articles)} transcripts")

        for article in articles:
            period = resolve_period(ticker, article, cur)
            if not period:
                print(f"  Skipping '{article['title']}' — could not determine period")
                continue

            try:
                transcript_text = get_transcript_content(article["id"])
            except Exception as e:
                print(f"  Error fetching transcript for '{article['title']}': {e}")
                continue

            if len(transcript_text) < 500:
                print(f"  Skipping '{article['title']}' — content too short ({len(transcript_text)} chars)")
                continue

            cur.execute(
                """
                INSERT INTO transcripts (ticker, period, transcript)
                VALUES (%s, %s, %s)
                ON CONFLICT (ticker, period)
                DO UPDATE SET transcript = EXCLUDED.transcript
                """,
                (ticker, period, transcript_text),
            )

            print(
                f"  Stored: {article['title']} -> period {period} "
                f"({len(transcript_text):,} chars)"
            )

            time.sleep(1)  # respect RapidAPI rate limits

    conn.commit()
    cur.close()
    conn.close()
    print("\nAll transcripts ingested successfully")


if __name__ == "__main__":
    ingest_transcripts()
