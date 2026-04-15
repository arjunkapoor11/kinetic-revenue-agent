"""
Kinetic Revenue Agent — Database Migration System

Manages schema migrations for PostgreSQL. Each migration is an idempotent SQL
statement with a unique ID. On every run, only unapplied migrations execute.

Usage:
    python setup_db.py                                      # Apply pending migrations
    python setup_db.py --status                             # Show migration status
    python setup_db.py --reset                              # Truncate x_* tables
    python setup_db.py --secrets kinetic-revenue-agent      # Use Secrets Manager
"""

import argparse
import psycopg2
from dotenv import load_dotenv
import os
import sys

from credentials import load_credentials, add_credentials_args

load_dotenv()


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        port=5432,
        sslmode="require",
    )


# ── migration definitions ────────────────────────────────────────────────────
# Each tuple: (migration_id, sql)
#
# Rules:
#   - IDs are immutable once deployed. Never rename or reorder.
#   - Each migration runs exactly once. Use IF NOT EXISTS / IF EXISTS guards
#     so replaying is safe even if schema_migrations is reset.
#   - To change existing tables, add a NEW migration with ALTER TABLE.
#   - Append only. Never edit a migration that has already shipped.

MIGRATIONS = [
    # ── revenue pipeline (original tables) ────────────────────────────────

    ("001_revenue_actuals", """
        CREATE TABLE IF NOT EXISTS revenue_actuals (
            id SERIAL PRIMARY KEY,
            ticker VARCHAR(10),
            period VARCHAR(20),
            revenue BIGINT,
            UNIQUE(ticker, period)
        );
    """),

    ("002_consensus_estimates", """
        CREATE TABLE IF NOT EXISTS consensus_estimates (
            id SERIAL PRIMARY KEY,
            ticker VARCHAR(10),
            period VARCHAR(20),
            estimated_revenue BIGINT,
            UNIQUE(ticker, period)
        );
    """),

    ("003_agent_reports", """
        CREATE TABLE IF NOT EXISTS agent_reports (
            id SERIAL PRIMARY KEY,
            ticker VARCHAR(10),
            report TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
    """),

    ("004_transcripts", """
        CREATE TABLE IF NOT EXISTS transcripts (
            id SERIAL PRIMARY KEY,
            ticker VARCHAR(10),
            period VARCHAR(20),
            transcript TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(ticker, period)
        );
    """),

    ("005_pre_earnings_consensus", """
        CREATE TABLE IF NOT EXISTS pre_earnings_consensus (
            id SERIAL PRIMARY KEY,
            ticker VARCHAR(10),
            period VARCHAR(20),
            estimated_revenue BIGINT,
            UNIQUE(ticker, period)
        );
    """),

    ("006_ticker_projections", """
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
    """),

    # ── x sentiment tracker (base tables) ─────────────────────────────────

    ("007_x_posts", """
        CREATE TABLE IF NOT EXISTS x_posts (
            post_id VARCHAR(30) PRIMARY KEY,
            text TEXT NOT NULL,
            author VARCHAR(100),
            author_followers INTEGER DEFAULT 0,
            timestamp TIMESTAMPTZ,
            provider VARCHAR(30),
            model_version VARCHAR(50),
            likes INTEGER DEFAULT 0,
            retweets INTEGER DEFAULT 0,
            raw_json JSONB
        );
    """),

    ("008_x_sentiment", """
        CREATE TABLE IF NOT EXISTS x_sentiment (
            post_id VARCHAR(30) PRIMARY KEY REFERENCES x_posts(post_id),
            sentiment_score NUMERIC(4,3),
            category VARCHAR(30),
            model_version_extracted VARCHAR(50),
            key_quote VARCHAR(200),
            analyzed_at TIMESTAMPTZ DEFAULT NOW()
        );
    """),

    ("009_x_daily_summary", """
        CREATE TABLE IF NOT EXISTS x_daily_summary (
            date DATE,
            provider VARCHAR(30),
            model_version VARCHAR(50),
            avg_sentiment NUMERIC(4,3),
            post_count INTEGER DEFAULT 0,
            praise_count INTEGER DEFAULT 0,
            complaint_count INTEGER DEFAULT 0,
            bug_count INTEGER DEFAULT 0,
            top_quote VARCHAR(200),
            PRIMARY KEY (date, provider, model_version)
        );
    """),

    ("010_x_last_run", """
        CREATE TABLE IF NOT EXISTS x_last_run (
            pipeline_name VARCHAR(50) PRIMARY KEY,
            last_run_at TIMESTAMPTZ NOT NULL
        );
    """),

    ("011_x_tracked_accounts", """
        CREATE TABLE IF NOT EXISTS x_tracked_accounts (
            account_id VARCHAR(30) PRIMARY KEY,
            username VARCHAR(100) NOT NULL,
            follower_count INTEGER DEFAULT 0,
            why_tracked TEXT,
            added_at TIMESTAMPTZ DEFAULT NOW(),
            last_updated TIMESTAMPTZ DEFAULT NOW()
        );
    """),

    # ── x sentiment tracker (investment signal columns) ───────────────────

    ("012_x_sentiment_add_switching_and_relevance", """
        ALTER TABLE x_sentiment
            ADD COLUMN IF NOT EXISTS switching_signal VARCHAR(20) DEFAULT 'none',
            ADD COLUMN IF NOT EXISTS investment_relevance NUMERIC(3,2) DEFAULT 0.0;
    """),

    ("013_x_daily_summary_add_switching_and_relevance", """
        ALTER TABLE x_daily_summary
            ADD COLUMN IF NOT EXISTS switching_to_count INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS switching_from_count INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS avg_investment_relevance NUMERIC(3,2) DEFAULT 0.0,
            ADD COLUMN IF NOT EXISTS top_investment_post VARCHAR(200);
    """),

    ("014_x_email_reports", """
        CREATE TABLE IF NOT EXISTS x_email_reports (
            date DATE PRIMARY KEY,
            email_html TEXT NOT NULL,
            generated_at TIMESTAMPTZ DEFAULT NOW()
        );
    """),

    ("015_x_posts_add_post_url", """
        ALTER TABLE x_posts
            ADD COLUMN IF NOT EXISTS post_url VARCHAR(100);
    """),

    ("016_x_distribution_list", """
        CREATE TABLE IF NOT EXISTS x_distribution_list (
            email VARCHAR(255) PRIMARY KEY,
            name VARCHAR(100),
            added_at TIMESTAMPTZ DEFAULT NOW(),
            active BOOLEAN DEFAULT TRUE
        );
    """),
]


# ── x_* tables subject to --reset ─────────────────────────────────────────

X_TABLES = [
    "x_distribution_list",
    "x_email_reports",
    "x_sentiment",      # FK first
    "x_daily_summary",
    "x_last_run",
    "x_tracked_accounts",
    "x_posts",          # parent last (FK constraint)
]

X_MIGRATION_PREFIX = "007_x"  # migrations 007+ are x_* tables


# ── migration engine ──────────────────────────────────────────────────────────

def ensure_schema_migrations(conn):
    """Create the schema_migrations table if it doesn't exist."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id VARCHAR(100) PRIMARY KEY,
            applied_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    conn.commit()
    cur.close()


def get_applied_migrations(conn):
    """Return the set of migration IDs that have already been applied."""
    cur = conn.cursor()
    cur.execute("SELECT migration_id FROM schema_migrations")
    applied = {row[0] for row in cur.fetchall()}
    cur.close()
    return applied


def run_migrations(conn):
    """Apply any pending migrations in order."""
    ensure_schema_migrations(conn)
    applied = get_applied_migrations(conn)

    pending = [(mid, sql) for mid, sql in MIGRATIONS if mid not in applied]

    if not pending:
        print("[migrations] All migrations already applied")
        return 0

    for mid, sql in pending:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (migration_id) VALUES (%s)",
                (mid,),
            )
            conn.commit()
            print(f"  [applied] {mid}")
        except Exception as e:
            conn.rollback()
            print(f"  [FAILED]  {mid}: {e}")
            cur.close()
            return 1
        cur.close()

    print(f"[migrations] Applied {len(pending)} migration(s)")
    return 0


def show_status(conn):
    """Print which migrations have been applied and which are pending."""
    ensure_schema_migrations(conn)
    applied = get_applied_migrations(conn)

    print(f"{'Status':<10} {'Migration ID'}")
    print("-" * 60)
    for mid, _ in MIGRATIONS:
        status = "applied" if mid in applied else "PENDING"
        print(f"{status:<10} {mid}")

    applied_count = sum(1 for mid, _ in MIGRATIONS if mid in applied)
    pending_count = len(MIGRATIONS) - applied_count
    print(f"\n{applied_count} applied, {pending_count} pending")


def reset_x_tables(conn):
    """Truncate all x_* tables and remove their migration records.

    Does NOT touch revenue pipeline tables (revenue_actuals, consensus_estimates,
    agent_reports, transcripts, pre_earnings_consensus, ticker_projections).
    """
    cur = conn.cursor()

    # Truncate x_* tables in FK-safe order
    for table in X_TABLES:
        cur.execute(f"TRUNCATE TABLE {table} CASCADE")
        print(f"  [truncated] {table}")

    # Remove x_* migration records so they re-run cleanly
    x_migration_ids = [mid for mid, _ in MIGRATIONS if mid >= X_MIGRATION_PREFIX]
    if x_migration_ids:
        cur.execute(
            "DELETE FROM schema_migrations WHERE migration_id = ANY(%s)",
            (x_migration_ids,),
        )
        print(f"  [reset] Removed {len(x_migration_ids)} x_* migration records")

    conn.commit()
    cur.close()
    print("[reset] x_* tables truncated — run setup_db.py again to re-apply migrations")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kinetic Revenue Agent — DB Migrations")
    parser.add_argument("--status", action="store_true",
                        help="Show migration status")
    parser.add_argument("--reset", action="store_true",
                        help="Truncate x_* tables and reset their migrations")
    add_credentials_args(parser)
    args = parser.parse_args()

    load_credentials(secret_name=args.secrets, region=args.region)

    conn = get_db_connection()

    if args.status:
        show_status(conn)
    elif args.reset:
        print("[reset] This will TRUNCATE all x_* tables (x_posts, x_sentiment, "
              "x_daily_summary, x_last_run, x_tracked_accounts).")
        print("[reset] Revenue pipeline tables are NOT affected.")
        confirm = input("Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("[reset] Aborted")
            conn.close()
            sys.exit(0)
        reset_x_tables(conn)
        # Re-apply migrations so tables exist (empty) after reset
        run_migrations(conn)
    else:
        exit_code = run_migrations(conn)
        conn.close()
        sys.exit(exit_code)

    conn.close()


if __name__ == "__main__":
    main()
