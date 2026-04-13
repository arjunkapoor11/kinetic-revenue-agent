import psycopg2
from dotenv import load_dotenv
import os

load_dotenv()

conn = psycopg2.connect(
    host=os.getenv("DB_HOST"),
    database=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    port=5432,
    sslmode="require"
)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS revenue_actuals (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(10),
    period VARCHAR(20),
    revenue BIGINT,
    UNIQUE(ticker, period)
);

CREATE TABLE IF NOT EXISTS consensus_estimates (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(10),
    period VARCHAR(20),
    estimated_revenue BIGINT,
    UNIQUE(ticker, period)
);

CREATE TABLE IF NOT EXISTS agent_reports (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(10),
    report TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS transcripts (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(10),
    period VARCHAR(20),
    transcript TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(ticker, period)
);

CREATE TABLE IF NOT EXISTS pre_earnings_consensus (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(10),
    period VARCHAR(20),
    estimated_revenue BIGINT,
    UNIQUE(ticker, period)
);

CREATE TABLE IF NOT EXISTS ticker_projections (
    ticker VARCHAR(10),
    period VARCHAR(20),
    projected_revenue NUMERIC,
    projected_qoq NUMERIC,
    method VARCHAR(50),
    beat_cadence NUMERIC,
    beat_window VARCHAR(10),
    momentum VARCHAR(20),
    created_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (ticker, period)
);
""")

conn.commit()
cur.close()
conn.close()
print("Tables created successfully")