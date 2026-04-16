"""
Kinetic Revenue Agent — X (Twitter) Sentiment Tracker

Searches X for developer mentions of AI model providers, filters for
developer signal, uses Claude Sonnet to extract sentiment, stores in
PostgreSQL, and posts daily summaries to Slack + email.

Hybrid strategy:
  - Tracked accounts (top 100 AI devs): all posts, no engagement filter
  - Keyword search: engagement filter (5+ likes OR 2+ retweets)
  - Incremental: only fetches posts newer than last run

Usage:
    python x_sentiment_tracker.py                  # Full run
    python x_sentiment_tracker.py --test           # Test mode (5 posts per provider)
    python x_sentiment_tracker.py --backfill       # Re-summarize without fetching
    python x_sentiment_tracker.py --refresh-accounts  # Force refresh tracked accounts
"""

import argparse
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict
from email import encoders
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import anthropic
import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

# credentials: load_credentials, add_credentials_args, auto_deploy
from credentials import load_credentials, add_credentials_args

load_dotenv()


# ── config ────────────────────────────────────────────────────────────────────

SLACK_CHANNEL = "#software-dashboard"

CLAUDE_MODEL = "claude-sonnet-4-20250514"

_PROJECT_DIR = Path(__file__).parent
_ANALYSIS_PROMPT_PATH = _PROJECT_DIR / "skills" / "x-sentiment-analysis.md"
_EMAIL_REPORT_PROMPT_PATH = _PROJECT_DIR / "skills" / "x_email_report.md"
_analysis_prompt_cache = None
_email_report_prompt_cache = None


def _load_analysis_prompt():
    """Load and cache the sentiment analysis prompt from the skills file."""
    global _analysis_prompt_cache
    if _analysis_prompt_cache is None:
        _analysis_prompt_cache = _ANALYSIS_PROMPT_PATH.read_text(encoding="utf-8")
    return _analysis_prompt_cache


def _load_email_report_prompt():
    """Load and cache the email report prompt from the skills file."""
    global _email_report_prompt_cache
    if _email_report_prompt_cache is None:
        _email_report_prompt_cache = _EMAIL_REPORT_PROMPT_PATH.read_text(encoding="utf-8")
    return _email_report_prompt_cache


def reload_email_prompt():
    """Clear the cached email report prompt so the next call re-reads from disk."""
    global _email_report_prompt_cache
    _email_report_prompt_cache = None

# Providers: provider-level search phrases only. No hardcoded model versions —
# Claude extracts specific versions from post text during analysis.
PROVIDERS = {
    "Anthropic": {
        "queries": ["Anthropic API", "Claude API", "Claude model"],
    },
    "OpenAI": {
        "queries": ["OpenAI API", "GPT API", "OpenAI model"],
    },
    "Google": {
        "queries": ["Gemini API", "Google AI API"],
    },
    "Meta": {
        "queries": ["Llama API", "Meta AI model"],
    },
    "xAI": {
        "queries": ["Grok API", "xAI model"],
    },
    "Mistral": {
        "queries": ["Mistral API", "Mistral model"],
    },
    "DeepSeek": {
        "queries": ["DeepSeek API", "DeepSeek model"],
    },
    "Qwen": {
        "queries": ["Qwen API", "Alibaba AI model"],
    },
}

# Consumer content blocklist — posts containing these are excluded before
# the developer keyword check. Catches image-gen, crypto, and fan content.
CONSUMER_BLOCKLIST = [
    "portrait", "realistic photo", "cinematic", "generate image",
    "draw me", "token price", "crypto", "nft", "kpop", "aespa",
]

# Developer signal keywords — posts must contain at least 2 to pass filter
DEV_KEYWORDS = [
    "api", "sdk", "token", "latency", "context window",
    "prompt", "rate limit", "fine-tuning", "inference",
    "benchmark", "deployment", "endpoint", "integration",
    "hallucination", "rag", "embeddings", "agent",
    "pipeline", "tool use", "model weights",
]

TRACKED_ACCOUNTS_REFRESH_DAYS = 7
TRACKED_ACCOUNTS_MAX = 150
MIN_FOLLOWER_COUNT = 500


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        port=5432,
        sslmode="require",
    )


# ── database setup ────────────────────────────────────────────────────────────

def ensure_tables():
    """Run pending database migrations to ensure all tables exist."""
    sys.path.insert(0, str(_PROJECT_DIR))
    from setup_db import get_db_connection as _get_conn, run_migrations
    conn = _get_conn()
    run_migrations(conn)
    conn.close()


# ── incremental fetching ─────────────────────────────────────────────────────

def get_last_run(conn, pipeline_name="x_sentiment"):
    """Return the last successful run timestamp, or None if first run."""
    cur = conn.cursor()
    cur.execute(
        "SELECT last_run_at FROM x_last_run WHERE pipeline_name = %s",
        (pipeline_name,),
    )
    row = cur.fetchone()
    cur.close()
    return row[0] if row else None


def update_last_run(conn, pipeline_name="x_sentiment"):
    """Set the last run timestamp to now."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO x_last_run (pipeline_name, last_run_at)
        VALUES (%s, NOW())
        ON CONFLICT (pipeline_name)
        DO UPDATE SET last_run_at = NOW()
    """, (pipeline_name,))
    conn.commit()
    cur.close()


# ── X API helpers ─────────────────────────────────────────────────────────────

def _x_get(url, bearer, params, label=""):
    """Make a GET request to X API with rate-limit handling. Returns JSON or None."""
    headers = {"Authorization": f"Bearer {bearer}"}

    resp = requests.get(url, headers=headers, params=params, timeout=30)

    if resp.status_code == 429:
        reset = int(resp.headers.get("x-rate-limit-reset", time.time() + 60))
        wait = max(reset - int(time.time()), 1)
        print(f"  [x-api] Rate limited{' (' + label + ')' if label else ''}, waiting {wait}s")
        time.sleep(wait)
        resp = requests.get(url, headers=headers, params=params, timeout=30)

    if resp.status_code != 200:
        print(f"  [x-api] Error {resp.status_code}{' (' + label + ')' if label else ''}: "
              f"{resp.text[:200]}")
        return None

    return resp.json()


def _parse_tweet_response(data, provider):
    """Parse a Twitter API v2 response into a list of post dicts."""
    tweets = data.get("data", [])
    if not tweets:
        return []

    users = {}
    for u in data.get("includes", {}).get("users", []):
        users[u["id"]] = {
            "username": u.get("username", ""),
            "followers": u.get("public_metrics", {}).get("followers_count", 0),
            "account_id": u["id"],
        }

    posts = []
    for tw in tweets:
        metrics = tw.get("public_metrics", {})
        author_info = users.get(tw.get("author_id"), {})
        posts.append({
            "post_id": tw["id"],
            "text": tw.get("text", ""),
            "author": author_info.get("username", ""),
            "author_followers": author_info.get("followers", 0),
            "author_id": author_info.get("account_id", tw.get("author_id", "")),
            "timestamp": tw.get("created_at"),
            "provider": provider,
            "likes": metrics.get("like_count", 0),
            "retweets": metrics.get("retweet_count", 0),
            "raw_json": tw,
        })
    return posts


# ── X API search: keyword queries ────────────────────────────────────────────

def _build_provider_query(search_phrase):
    """Build an X search query from a provider-level phrase + dev signal terms."""
    dev_terms = ["API", "SDK", "latency", "token", "context window",
                 "prompt", "rate limit", "fine-tuning", "inference",
                 "benchmark", "endpoint", "agent"]
    dev_part = " OR ".join(dev_terms)
    return f"\"{search_phrase}\" ({dev_part}) -is:retweet lang:en"


def search_keyword_posts(bearer, provider, provider_info, since=None,
                         max_results=100, test_mode=False):
    """Run provider-level keyword searches (no hardcoded model versions).

    Applies engagement filter: 5+ likes OR 2+ retweets (for non-tracked accounts).
    Returns deduplicated list of post dicts.
    """
    url = "https://api.twitter.com/2/tweets/search/recent"
    base_params = {
        "tweet.fields": "created_at,public_metrics,author_id",
        "expansions": "author_id",
        "user.fields": "username,public_metrics",
    }
    if since:
        base_params["start_time"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    seen_ids = set()
    all_posts = []

    for phrase in provider_info["queries"]:
        query = _build_provider_query(phrase)
        limit = 10 if test_mode else min(max_results, 100)
        params = {**base_params, "query": query, "max_results": limit}

        data = _x_get(url, bearer, params, label=f"{provider}/{phrase}")
        if not data:
            continue

        posts = _parse_tweet_response(data, provider)

        # Pagination for non-test mode
        if not test_mode:
            next_token = data.get("meta", {}).get("next_token")
            while next_token:
                params["next_token"] = next_token
                data = _x_get(url, bearer, params,
                              label=f"{provider}/{phrase}")
                if not data:
                    break
                posts.extend(_parse_tweet_response(data, provider))
                next_token = data.get("meta", {}).get("next_token")
                time.sleep(1)

        min_likes = 1 if test_mode else 5
        min_retweets = 1 if test_mode else 2

        for p in posts:
            if p["post_id"] in seen_ids:
                continue
            seen_ids.add(p["post_id"])
            if p["likes"] >= min_likes or p["retweets"] >= min_retweets:
                all_posts.append(p)

        time.sleep(0.5)

    print(f"[x-api] {provider} keyword search: {len(all_posts)} posts "
          f"(engagement-filtered from {len(seen_ids)} total)")
    return all_posts


# ── X API search: tracked accounts ───────────────────────────────────────────

def search_tracked_account_posts(bearer, conn, since=None, test_mode=False):
    """Fetch recent posts from all tracked accounts. No engagement filter.

    Batches usernames into queries of ~15 per request to stay within
    X API query length limits.
    """
    cur = conn.cursor()
    cur.execute("SELECT username FROM x_tracked_accounts ORDER BY follower_count DESC")
    usernames = [r[0] for r in cur.fetchall()]
    cur.close()

    if not usernames:
        print("[x-api] No tracked accounts — skipping account search")
        return []

    url = "https://api.twitter.com/2/tweets/search/recent"
    base_params = {
        "tweet.fields": "created_at,public_metrics,author_id",
        "expansions": "author_id",
        "user.fields": "username,public_metrics",
        "max_results": 10 if test_mode else 100,
    }
    if since:
        base_params["start_time"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Extract the key noun from each provider's first query phrase
    # e.g. "Anthropic API" -> "Anthropic", "Claude API" -> "Claude"
    all_model_terms = set()
    for info in PROVIDERS.values():
        for phrase in info["queries"]:
            key_word = phrase.split()[0]  # "Claude" from "Claude API"
            all_model_terms.add(key_word)
    model_part = " OR ".join(sorted(all_model_terms))

    all_posts = []
    seen_ids = set()
    batch_size = 15

    for i in range(0, len(usernames), batch_size):
        batch = usernames[i : i + batch_size]
        from_part = " OR ".join(f"from:{u}" for u in batch)
        query = f"({from_part}) ({model_part}) -is:retweet lang:en"

        params = {**base_params, "query": query}
        data = _x_get(url, bearer, params, label=f"tracked batch {i // batch_size + 1}")
        if not data:
            continue

        # All providers will be resolved per-post later; tag as "tracked" for now
        posts = _parse_tweet_response(data, provider="__tracked__")

        for p in posts:
            if p["post_id"] not in seen_ids:
                seen_ids.add(p["post_id"])
                all_posts.append(p)

        if test_mode:
            break

        time.sleep(1)

    # Resolve provider from post text
    for p in all_posts:
        p["provider"] = _detect_provider(p["text"])

    print(f"[x-api] Tracked accounts: {len(all_posts)} posts from "
          f"{len(usernames)} accounts")
    return all_posts


def _detect_provider(text):
    """Detect which provider a post is about from its text."""
    text_lower = text.lower()
    for provider, info in PROVIDERS.items():
        for phrase in info["queries"]:
            # Match on the key noun in each phrase (e.g. "Claude" from "Claude API")
            for word in phrase.split():
                if word.lower() not in ("api", "model", "ai") and word.lower() in text_lower:
                    return provider
    return "unknown"


# ── tracked account management ────────────────────────────────────────────────

def _needs_account_refresh(conn):
    """Return True if tracked accounts need refreshing (empty or >7 days old)."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*), MAX(last_updated) FROM x_tracked_accounts")
    count, latest = cur.fetchone()
    cur.close()

    if count == 0:
        return True
    if latest is None:
        return True
    age = datetime.now(timezone.utc) - latest.replace(tzinfo=timezone.utc)
    return age.days >= TRACKED_ACCOUNTS_REFRESH_DAYS


def discover_tracked_accounts(conn, bearer, test_mode=False):
    """Search X for influential AI/ML developer accounts.

    Uses broad queries to find developers, then Claude ranks and selects
    the top 150. Uses a low follower floor (100) during discovery since
    Claude will filter for quality.
    """
    print("[accounts] Discovering influential AI developer accounts...")

    discovery_queries = [
        "Claude API developer",
        "OpenAI API engineer",
        "LLM inference production",
        "building with Anthropic",
        "building with OpenAI",
    ]

    # Low floor during discovery — Claude ranks for quality later
    discovery_min_followers = 100

    url = "https://api.twitter.com/2/tweets/search/recent"
    candidates = {}  # account_id -> {username, followers, sample_tweets}

    limit = 2 if test_mode else len(discovery_queries)

    for query_text in discovery_queries[:limit]:
        params = {
            "query": f"{query_text} -is:retweet lang:en",
            "max_results": 100,
            "tweet.fields": "created_at,public_metrics,author_id",
            "expansions": "author_id",
            "user.fields": "username,public_metrics,description",
        }

        data = _x_get(url, bearer, params, label=f"discovery/{query_text}")
        if not data:
            continue

        users = {}
        for u in data.get("includes", {}).get("users", []):
            users[u["id"]] = {
                "username": u.get("username", ""),
                "followers": u.get("public_metrics", {}).get("followers_count", 0),
                "description": u.get("description", ""),
                "account_id": u["id"],
            }

        for tw in data.get("data", []):
            author_id = tw.get("author_id")
            user = users.get(author_id)
            if not user or user["followers"] < discovery_min_followers:
                continue

            aid = user["account_id"]
            if aid not in candidates:
                candidates[aid] = {
                    "account_id": aid,
                    "username": user["username"],
                    "followers": user["followers"],
                    "description": user["description"],
                    "sample_tweets": [],
                }
            if len(candidates[aid]["sample_tweets"]) < 3:
                candidates[aid]["sample_tweets"].append(tw.get("text", "")[:200])

        time.sleep(1)

    print(f"[accounts] Found {len(candidates)} candidate accounts with "
          f"{discovery_min_followers}+ followers")

    if not candidates:
        return

    # Use Claude to rank candidates and select top 100
    ranked = _rank_accounts_with_claude(list(candidates.values()), test_mode)

    # Store in DB
    cur = conn.cursor()
    for acct in ranked:
        cur.execute("""
            INSERT INTO x_tracked_accounts (account_id, username, follower_count,
                                            why_tracked, added_at, last_updated)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (account_id)
            DO UPDATE SET follower_count = EXCLUDED.follower_count,
                          why_tracked = EXCLUDED.why_tracked,
                          last_updated = NOW()
        """, (
            acct["account_id"], acct["username"], acct["followers"],
            acct.get("why_tracked", ""),
        ))
    conn.commit()
    cur.close()
    print(f"[accounts] Stored {len(ranked)} tracked accounts")


def _rank_accounts_with_claude(candidates, test_mode=False):
    """Use Claude to rank candidate accounts and select top 100."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Build concise candidate list for Claude
    candidate_text = ""
    for c in candidates[:200]:  # Cap input to avoid huge prompts
        tweets_preview = " | ".join(c["sample_tweets"][:2])
        candidate_text += (
            f"@{c['username']} ({c['followers']} followers): "
            f"{c['description'][:100]}. "
            f"Tweets: {tweets_preview[:200]}\n"
        )

    prompt = f"""Review these X (Twitter) accounts and select the top {TRACKED_ACCOUNTS_MAX} most valuable
to track for AI/LLM developer sentiment. We want accounts that:
- Regularly post about using LLM APIs (Claude, GPT, Gemini, etc.) in production
- Share technical insights about model performance, latency, pricing, capabilities
- Are actual developers/engineers, not just news aggregators or marketing accounts
- Have meaningful follower counts indicating influence in the AI dev community

For each selected account, provide a brief reason why they're worth tracking.

Return a JSON array of objects: {{"username": "...", "why_tracked": "..."}}
Select up to {TRACKED_ACCOUNTS_MAX} accounts, ordered by value. Return ONLY the JSON array.

Candidates:
{candidate_text}"""

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if "```" in text:
                text = text[: text.rfind("```")]
        ranked = json.loads(text)
    except (json.JSONDecodeError, IndexError, anthropic.APIError) as e:
        print(f"  [claude] Account ranking failed: {e}")
        # Fallback: take top 100 by follower count
        ranked = [{"username": c["username"], "why_tracked": "high follower count"}
                  for c in sorted(candidates, key=lambda x: x["followers"], reverse=True)
                  [:TRACKED_ACCOUNTS_MAX]]

    # Merge back account_ids and follower counts from candidates
    by_username = {c["username"].lower(): c for c in candidates}
    result = []
    for r in ranked[:TRACKED_ACCOUNTS_MAX]:
        username = r.get("username", "").lstrip("@")
        c = by_username.get(username.lower())
        if c:
            result.append({
                "account_id": c["account_id"],
                "username": c["username"],
                "followers": c["followers"],
                "why_tracked": r.get("why_tracked", ""),
            })

    return result


def review_new_accounts(conn, posts):
    """Review authors from keyword search results and flag accounts worth tracking.

    Called after keyword search. Finds authors with 1000+ followers not already
    tracked, and uses Claude to decide if they should be added.
    """
    cur = conn.cursor()
    cur.execute("SELECT account_id FROM x_tracked_accounts")
    tracked_ids = {r[0] for r in cur.fetchall()}
    cur.close()

    # Collect new high-follower accounts
    new_accounts = {}
    for p in posts:
        aid = p.get("author_id", "")
        if not aid or aid in tracked_ids:
            continue
        if p["author_followers"] < MIN_FOLLOWER_COUNT:
            continue
        if aid not in new_accounts:
            new_accounts[aid] = {
                "account_id": aid,
                "username": p["author"],
                "followers": p["author_followers"],
                "sample_tweets": [],
            }
        if len(new_accounts[aid]["sample_tweets"]) < 3:
            new_accounts[aid]["sample_tweets"].append(p["text"][:200])

    if not new_accounts:
        return

    print(f"[accounts] Reviewing {len(new_accounts)} new candidate accounts")

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    candidate_text = ""
    for c in list(new_accounts.values())[:50]:
        tweets = " | ".join(c["sample_tweets"])
        candidate_text += f"@{c['username']} ({c['followers']} followers): {tweets[:200]}\n"

    prompt = f"""Review these new X accounts found in AI/LLM developer searches.
Which ones are actual AI developers worth tracking for ongoing LLM sentiment?

Return a JSON array of objects for accounts worth adding:
{{"username": "...", "why_tracked": "..."}}

Return an empty array [] if none are worth tracking. Return ONLY the JSON array.

Candidates:
{candidate_text}"""

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if "```" in text:
                text = text[: text.rfind("```")]
        additions = json.loads(text)
    except (json.JSONDecodeError, IndexError, anthropic.APIError) as e:
        print(f"  [claude] Account review failed: {e}")
        return

    if not additions:
        print("[accounts] No new accounts worth tracking")
        return

    by_username = {c["username"].lower(): c for c in new_accounts.values()}
    cur = conn.cursor()
    added = 0
    for a in additions:
        username = a.get("username", "").lstrip("@")
        c = by_username.get(username.lower())
        if not c:
            continue
        cur.execute("""
            INSERT INTO x_tracked_accounts (account_id, username, follower_count,
                                            why_tracked, added_at, last_updated)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (account_id) DO NOTHING
        """, (c["account_id"], c["username"], c["followers"],
              a.get("why_tracked", "")))
        added += 1
    conn.commit()
    cur.close()
    print(f"[accounts] Added {added} new tracked accounts")


# ── content filters ──────────────────────────────────────────────────────────

def _is_consumer_content(text):
    """Return True if the post matches the consumer/spam blocklist."""
    text_lower = text.lower()
    return any(term in text_lower for term in CONSUMER_BLOCKLIST)


def is_developer_signal(text, min_keywords=2):
    """Return True if the post contains at least min_keywords developer keywords."""
    text_lower = text.lower()
    hits = sum(1 for kw in DEV_KEYWORDS if kw in text_lower)
    return hits >= min_keywords


def filter_developer_posts(posts, test_mode=False, tracked_account_ids=None):
    """Filter posts: remove consumer content, then require dev keywords.

    In test mode, requires 1 keyword. In production, requires 2 — but
    tracked accounts only need 1 keyword since they're pre-vetted developers.
    """
    default_min = 1 if test_mode else 2
    tracked = tracked_account_ids or set()

    before = len(posts)
    posts = [p for p in posts if not _is_consumer_content(p["text"])]
    blocked = before - len(posts)

    filtered = []
    for p in posts:
        min_kw = 1 if p.get("author_id", "") in tracked else default_min
        if is_developer_signal(p["text"], min_kw):
            filtered.append(p)
    dropped = len(posts) - len(filtered)

    if blocked or dropped:
        print(f"  [filter] Kept {len(filtered)}, blocked {blocked} consumer, "
              f"dropped {dropped} (need {default_min}+ dev keywords, "
              f"1 for tracked accounts)")
    return filtered


# ── deduplication ─────────────────────────────────────────────────────────────

def get_existing_post_ids(conn, post_ids):
    """Return the set of post_ids that already exist in x_posts."""
    if not post_ids:
        return set()
    cur = conn.cursor()
    cur.execute(
        "SELECT post_id FROM x_posts WHERE post_id = ANY(%s)",
        (list(post_ids),),
    )
    existing = {r[0] for r in cur.fetchall()}
    cur.close()
    return existing


def deduplicate_posts(conn, posts):
    """Remove posts we've already stored. Returns only new posts."""
    ids = [p["post_id"] for p in posts]
    existing = get_existing_post_ids(conn, ids)
    new_posts = [p for p in posts if p["post_id"] not in existing]
    if existing:
        print(f"  [dedup] Skipped {len(existing)} already-seen posts")
    return new_posts


# ── Claude sentiment analysis ────────────────────────────────────────────────

def analyze_posts_with_claude(posts, provider):
    """Use Claude Sonnet to analyze sentiment for a batch of posts.

    Returns a list of analysis dicts, one per post.
    """
    if not posts:
        return []

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    results = []
    batch_size = 20

    for i in range(0, len(posts), batch_size):
        batch = posts[i : i + batch_size]

        posts_text = ""
        for idx, p in enumerate(batch):
            posts_text += (
                f"POST {idx + 1} (id={p['post_id']}):\n"
                f"Author: @{p['author']} ({p['author_followers']} followers)\n"
                f"Text: {p['text']}\n"
                f"Likes: {p['likes']} | Retweets: {p['retweets']}\n\n"
            )

        # Load analysis framework from skills file, inject provider + posts
        framework = _load_analysis_prompt()
        framework = framework.replace("{provider}", provider)
        prompt = f"{framework}\n\nPosts:\n{posts_text}"

        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()

            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                if text.endswith("```"):
                    text = text[: text.rfind("```")]
            parsed = json.loads(text)
            results.extend(parsed)
        except (json.JSONDecodeError, IndexError, KeyError) as e:
            print(f"  [claude] Parse error for {provider} batch {i}: {e}")
            for p in batch:
                results.append({
                    "post_id": p["post_id"],
                    "sentiment_score": 0.0,
                    "category": "capability",
                    "model_version_extracted": provider,
                    "key_quote": p["text"][:120],
                    "switching_signal": "none",
                    "investment_relevance": 0.0,
                })
        except anthropic.APIError as e:
            print(f"  [claude] API error for {provider} batch {i}: {e}")
            for p in batch:
                results.append({
                    "post_id": p["post_id"],
                    "sentiment_score": 0.0,
                    "category": "capability",
                    "model_version_extracted": provider,
                    "key_quote": p["text"][:120],
                    "switching_signal": "none",
                    "investment_relevance": 0.0,
                })

        if i + batch_size < len(posts):
            time.sleep(0.5)

    print(f"  [claude] Analyzed {len(results)} posts for {provider}")
    return results


# ── database storage ──────────────────────────────────────────────────────────

def store_posts(conn, posts):
    """Insert new posts into x_posts."""
    if not posts:
        return
    cur = conn.cursor()
    for p in posts:
        post_url = f"https://x.com/i/web/status/{p['post_id']}"
        cur.execute("""
            INSERT INTO x_posts (post_id, text, author, author_followers, timestamp,
                                 provider, model_version, likes, retweets, raw_json,
                                 post_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (post_id) DO NOTHING
        """, (
            p["post_id"], p["text"], p["author"], p["author_followers"],
            p["timestamp"], p["provider"], p.get("model_version"),
            p["likes"], p["retweets"],
            json.dumps(p["raw_json"]) if p.get("raw_json") else None,
            post_url,
        ))
    conn.commit()
    cur.close()
    print(f"  [db] Stored {len(posts)} posts")


VALID_CATEGORIES = frozenset((
    "praise", "bug", "complaint", "capability", "pricing", "reliability", "switching",
))
VALID_SWITCHING = frozenset(("switching_to", "switching_from", "comparing", "none"))


def store_sentiments(conn, analyses):
    """Insert sentiment analysis results into x_sentiment."""
    if not analyses:
        return
    cur = conn.cursor()
    for a in analyses:
        score = max(-1.0, min(1.0, float(a.get("sentiment_score", 0))))
        category = a.get("category", "capability")
        if category not in VALID_CATEGORIES:
            category = "capability"
        switching = a.get("switching_signal", "none")
        if switching not in VALID_SWITCHING:
            switching = "none"
        relevance = max(0.0, min(1.0, float(a.get("investment_relevance", 0))))
        cur.execute("""
            INSERT INTO x_sentiment (post_id, sentiment_score, category,
                                     model_version_extracted, key_quote,
                                     switching_signal, investment_relevance,
                                     analyzed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (post_id) DO NOTHING
        """, (
            a["post_id"], score, category,
            a.get("model_version_extracted") or "unspecified",
            (a.get("key_quote") or "")[:200],
            switching, relevance,
        ))
    conn.commit()
    cur.close()
    print(f"  [db] Stored {len(analyses)} sentiment records")


# ── daily summary ─────────────────────────────────────────────────────────────

def build_daily_summary(conn, target_date=None):
    """Build and store the daily summary from x_posts + x_sentiment.

    Returns a list of summary dicts for Slack/email.
    """
    if target_date is None:
        target_date = datetime.now(ZoneInfo("America/New_York")).date()

    cur = conn.cursor()
    cur.execute("""
        SELECT
            p.provider,
            s.model_version_extracted,
            AVG(s.sentiment_score) AS avg_sentiment,
            COUNT(*) AS post_count,
            SUM(CASE WHEN s.category = 'praise' THEN 1 ELSE 0 END),
            SUM(CASE WHEN s.category = 'complaint' THEN 1 ELSE 0 END),
            SUM(CASE WHEN s.category = 'bug' THEN 1 ELSE 0 END),
            SUM(CASE WHEN s.switching_signal = 'switching_to' THEN 1 ELSE 0 END),
            SUM(CASE WHEN s.switching_signal = 'switching_from' THEN 1 ELSE 0 END),
            AVG(s.investment_relevance),
            (
                SELECT s2.key_quote
                FROM x_sentiment s2
                JOIN x_posts p2 ON s2.post_id = p2.post_id
                WHERE p2.provider = p.provider
                  AND s2.model_version_extracted = s.model_version_extracted
                  AND s2.analyzed_at::date = %s::date
                ORDER BY p2.likes + p2.retweets DESC
                LIMIT 1
            ) AS top_quote,
            (
                SELECT s3.key_quote
                FROM x_sentiment s3
                JOIN x_posts p3 ON s3.post_id = p3.post_id
                WHERE p3.provider = p.provider
                  AND s3.model_version_extracted = s.model_version_extracted
                  AND s3.analyzed_at::date = %s::date
                ORDER BY s3.investment_relevance DESC
                LIMIT 1
            ) AS top_investment_post
        FROM x_posts p
        JOIN x_sentiment s ON p.post_id = s.post_id
        WHERE s.analyzed_at::date = %s::date
        GROUP BY p.provider, s.model_version_extracted
        ORDER BY post_count DESC
    """, (target_date, target_date, target_date))

    summaries = []
    for row in cur.fetchall():
        summary = {
            "date": target_date,
            "provider": row[0],
            "model_version": row[1] or "unspecified",
            "avg_sentiment": round(float(row[2]), 3) if row[2] else 0.0,
            "post_count": row[3],
            "praise_count": row[4],
            "complaint_count": row[5],
            "bug_count": row[6],
            "switching_to_count": row[7],
            "switching_from_count": row[8],
            "avg_investment_relevance": round(float(row[9]), 2) if row[9] else 0.0,
            "top_quote": row[10] or "",
            "top_investment_post": row[11] or "",
        }
        summaries.append(summary)

    for s in summaries:
        cur.execute("""
            INSERT INTO x_daily_summary (date, provider, model_version, avg_sentiment,
                                         post_count, praise_count, complaint_count,
                                         bug_count, switching_to_count, switching_from_count,
                                         avg_investment_relevance, top_quote,
                                         top_investment_post)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (date, provider, model_version)
            DO UPDATE SET
                avg_sentiment = EXCLUDED.avg_sentiment,
                post_count = EXCLUDED.post_count,
                praise_count = EXCLUDED.praise_count,
                complaint_count = EXCLUDED.complaint_count,
                bug_count = EXCLUDED.bug_count,
                switching_to_count = EXCLUDED.switching_to_count,
                switching_from_count = EXCLUDED.switching_from_count,
                avg_investment_relevance = EXCLUDED.avg_investment_relevance,
                top_quote = EXCLUDED.top_quote,
                top_investment_post = EXCLUDED.top_investment_post
        """, (
            s["date"], s["provider"], s["model_version"], s["avg_sentiment"],
            s["post_count"], s["praise_count"], s["complaint_count"],
            s["bug_count"], s["switching_to_count"], s["switching_from_count"],
            s["avg_investment_relevance"], s["top_quote"],
            s["top_investment_post"],
        ))

    conn.commit()
    cur.close()

    print(f"[summary] Built {len(summaries)} provider/model summaries for {target_date}")
    return summaries


def get_daily_summary(target_date=None):
    """Fetch the daily summary for a given date. Used by the MCP tool."""
    if target_date is None:
        target_date = datetime.now(ZoneInfo("America/New_York")).date()
    elif isinstance(target_date, str):
        target_date = datetime.strptime(target_date, "%Y-%m-%d").date()

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT date, provider, model_version, avg_sentiment, post_count,
               praise_count, complaint_count, bug_count,
               switching_to_count, switching_from_count,
               avg_investment_relevance, top_quote, top_investment_post
        FROM x_daily_summary
        WHERE date = %s
        ORDER BY post_count DESC
    """, (target_date,))

    summaries = []
    for row in cur.fetchall():
        summaries.append({
            "date": str(row[0]),
            "provider": row[1],
            "model_version": row[2],
            "avg_sentiment": float(row[3]) if row[3] else 0.0,
            "post_count": row[4],
            "praise_count": row[5],
            "complaint_count": row[6],
            "bug_count": row[7],
            "switching_to_count": row[8] or 0,
            "switching_from_count": row[9] or 0,
            "avg_investment_relevance": float(row[10]) if row[10] else 0.0,
            "top_quote": row[11] or "",
            "top_investment_post": row[12] or "",
        })

    cur.close()
    conn.close()

    total_posts = sum(s["post_count"] for s in summaries)
    providers_seen = len(set(s["provider"] for s in summaries))
    overall_sentiment = (
        sum(s["avg_sentiment"] * s["post_count"] for s in summaries) / total_posts
        if total_posts > 0 else 0.0
    )

    return {
        "date": str(target_date),
        "total_posts": total_posts,
        "providers_tracked": providers_seen,
        "overall_sentiment": round(overall_sentiment, 3),
        "by_provider": summaries,
    }


# ── Slack notification ────────────────────────────────────────────────────────

def _sentiment_emoji(score):
    if score >= 0.3:
        return ":large_green_circle:"
    elif score <= -0.3:
        return ":red_circle:"
    return ":white_circle:"


def build_slack_message(summaries, target_date):
    """Build a formatted Slack message from daily summaries.

    Surfaces switching signals and top investment posts prominently,
    following the Daily Summary Priorities from the analysis framework.
    """
    date_str = (target_date.strftime("%B %d, %Y")
                if hasattr(target_date, "strftime") else str(target_date))
    total = sum(s["post_count"] for s in summaries)

    lines = []
    lines.append(f":bird: *X Developer Sentiment — {date_str}*")
    lines.append(f"_{total} developer posts analyzed across "
                 f"{len(set(s['provider'] for s in summaries))} providers_")
    lines.append("")

    # ── 1. Switching signals (highest priority) ──────────────────────────
    switching_items = [s for s in summaries
                       if s.get("switching_to_count", 0) > 0
                       or s.get("switching_from_count", 0) > 0]
    if switching_items:
        lines.append(":rotating_light: *Switching Signals*")
        for s in switching_items:
            parts = []
            if s.get("switching_to_count"):
                parts.append(f"+{s['switching_to_count']} switching TO")
            if s.get("switching_from_count"):
                parts.append(f"-{s['switching_from_count']} switching FROM")
            lines.append(f"  *{s['provider']}* {s['model_version']}: "
                         f"{', '.join(parts)}")
        lines.append("")

    # ── 2. Top investment posts (across all providers) ───────────────────
    top_invest = sorted(
        [s for s in summaries if s.get("top_investment_post")],
        key=lambda x: x.get("avg_investment_relevance", 0),
        reverse=True,
    )[:3]
    if top_invest:
        lines.append(":chart_with_upwards_trend: *Top Investment Signal*")
        for s in top_invest:
            lines.append(f"  *{s['provider']}* (relevance {s['avg_investment_relevance']:.1f}): "
                         f"_\"{s['top_investment_post'][:100]}\"_")
        lines.append("")

    # ── 3. Provider breakdown ────────────────────────────────────────────
    by_provider = defaultdict(list)
    for s in summaries:
        by_provider[s["provider"]].append(s)

    for provider in PROVIDERS:
        items = by_provider.get(provider, [])
        if not items:
            continue

        total_p = sum(i["post_count"] for i in items)
        avg_s = (sum(i["avg_sentiment"] * i["post_count"] for i in items) / total_p
                 if total_p else 0)
        emoji = _sentiment_emoji(avg_s)

        lines.append(f"{emoji} *{provider}* — {total_p} posts, "
                     f"avg sentiment: {avg_s:+.2f}")

        for item in sorted(items, key=lambda x: x["post_count"], reverse=True)[:3]:
            cat_counts = []
            if item["praise_count"]:
                cat_counts.append(f"{item['praise_count']} praise")
            if item["complaint_count"]:
                cat_counts.append(f"{item['complaint_count']} complaint")
            if item["bug_count"]:
                cat_counts.append(f"{item['bug_count']} bug")
            sw_to = item.get("switching_to_count", 0)
            sw_from = item.get("switching_from_count", 0)
            if sw_to:
                cat_counts.append(f"{sw_to} switch-to")
            if sw_from:
                cat_counts.append(f"{sw_from} switch-from")
            cats = ", ".join(cat_counts) if cat_counts else "mixed"
            lines.append(f"  `{item['model_version']:20s}` n={item['post_count']}  "
                         f"({cats})")
            if item.get("top_quote"):
                lines.append(f"    _\"{item['top_quote'][:100]}\"_")

        lines.append("")

    lines.append("_Kinetic X Sentiment Tracker — automated daily digest_")
    return "\n".join(lines)


def post_to_slack(message):
    """Post message to Slack webhook."""
    webhook = os.getenv("SLACK_WEBHOOK")
    if not webhook:
        print("[slack] No SLACK_WEBHOOK configured — printing only")
        print(message)
        return False

    payload = {
        "channel": SLACK_CHANNEL,
        "text": message,
        "unfurl_links": False,
    }
    resp = requests.post(webhook, json=payload, timeout=10)
    if resp.status_code == 200 and resp.text == "ok":
        print(f"[slack] Posted to {SLACK_CHANNEL}")
        return True
    else:
        print(f"[slack] Failed: {resp.status_code} {resp.text}")
        return False


# ── email report generation ───────────────────────────────────────────────

def _fetch_top_posts(conn, target_date, limit=5):
    """Fetch top posts by investment_relevance for the email report."""
    cur = conn.cursor()
    cur.execute("""
        SELECT p.post_id, p.text, p.author, p.author_followers, p.provider,
               p.likes, p.retweets, p.post_url,
               s.sentiment_score, s.category, s.model_version_extracted,
               s.key_quote, s.switching_signal, s.investment_relevance
        FROM x_posts p
        JOIN x_sentiment s ON p.post_id = s.post_id
        WHERE s.analyzed_at::date = %s::date
        ORDER BY s.investment_relevance DESC
        LIMIT %s
    """, (target_date, limit))
    posts = []
    for row in cur.fetchall():
        posts.append({
            "post_id": row[0],
            "text": row[1],
            "author": row[2],
            "author_followers": row[3],
            "provider": row[4],
            "likes": row[5],
            "retweets": row[6],
            "post_url": row[7] or f"https://x.com/i/web/status/{row[0]}",
            "sentiment_score": float(row[8]) if row[8] else 0.0,
            "category": row[9],
            "model_version": row[10],
            "key_quote": row[11],
            "switching_signal": row[12],
            "investment_relevance": float(row[13]) if row[13] else 0.0,
        })
    cur.close()
    return posts


def _fetch_7day_averages(conn, target_date):
    """Fetch 7-day rolling sentiment averages by provider."""
    cur = conn.cursor()
    cur.execute("""
        SELECT provider,
               AVG(avg_sentiment) AS rolling_avg,
               COUNT(DISTINCT date) AS days_of_data
        FROM x_daily_summary
        WHERE date BETWEEN %s::date - INTERVAL '7 days' AND %s::date - INTERVAL '1 day'
        GROUP BY provider
    """, (str(target_date), str(target_date)))
    result = {}
    for row in cur.fetchall():
        result[row[0]] = {
            "avg_sentiment_7d": round(float(row[1]), 3) if row[1] else None,
            "days_of_data": row[2],
        }
    cur.close()
    return result


def _fetch_total_ingested(conn, target_date):
    """Fetch raw ingested count vs filtered count for coverage summary."""
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM x_posts
        WHERE timestamp::date = %s::date
           OR (timestamp IS NULL
              AND post_id IN (
                  SELECT post_id FROM x_sentiment
                  WHERE analyzed_at::date = %s::date
              ))
    """, (target_date, target_date))
    total_ingested = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM x_sentiment
        WHERE analyzed_at::date = %s::date
    """, (target_date,))
    total_analyzed = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM x_sentiment
        WHERE analyzed_at::date = %s::date
          AND investment_relevance >= 0.6
    """, (target_date,))
    high_relevance = cur.fetchone()[0]
    cur.close()
    return total_ingested, total_analyzed, high_relevance


def generate_email_report(conn, target_date, summaries):
    """Generate a rich HTML email report using Claude and the email report framework.

    Returns the HTML string, or None if generation fails.
    """
    top_posts = _fetch_top_posts(conn, target_date, limit=5)
    rolling_avgs = _fetch_7day_averages(conn, target_date)
    total_ingested, total_analyzed, high_relevance = _fetch_total_ingested(
        conn, target_date)

    # Count tracked accounts
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM x_tracked_accounts")
    tracked_count = cur.fetchone()[0]

    # Fetch all analyzed posts for theme extraction and model version detection
    cur.execute("""
        SELECT p.text, p.author, p.provider,
               s.model_version_extracted, s.category
        FROM x_posts p
        JOIN x_sentiment s ON p.post_id = s.post_id
        WHERE s.analyzed_at::date = %s::date
        ORDER BY s.investment_relevance DESC
    """, (target_date,))
    all_posts_summary = [
        {"text": r[0][:300], "author": r[1], "provider": r[2],
         "model_version": r[3], "category": r[4]}
        for r in cur.fetchall()
    ]
    cur.close()

    # Build structured data payload for Claude
    data_payload = json.dumps({
        "date": str(target_date),
        "coverage": {
            "total_ingested": total_ingested,
            "passed_dev_filter": total_analyzed,
            "high_relevance": high_relevance,
            "tracked_accounts": tracked_count,
        },
        "provider_summaries": summaries,
        "rolling_7day_averages": rolling_avgs,
        "top_posts": top_posts,
        "all_posts_summary": all_posts_summary,
    }, indent=2, default=str)

    framework = _load_email_report_prompt()
    prompt = (
        f"{framework}\n\n"
        f"Today's date: {target_date}\n\n"
        f"Data:\n{data_payload}"
    )

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        html = response.content[0].text.strip()
        # Strip markdown code fences if present
        if html.startswith("```"):
            html = html.split("\n", 1)[1]
            if "```" in html:
                html = html[: html.rfind("```")]
        print(f"[email-report] Generated HTML report ({len(html)} chars)")
        print(f"[email-report] HTML preview:\n{html[:3000]}")
        return html
    except anthropic.APIError as e:
        print(f"[email-report] Claude API error: {e}")
        return None


def store_email_report(conn, target_date, email_html):
    """Store the generated email HTML in x_email_reports."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO x_email_reports (date, email_html, generated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (date)
        DO UPDATE SET email_html = EXCLUDED.email_html,
                      generated_at = NOW()
    """, (target_date, email_html))
    conn.commit()
    cur.close()
    print(f"[email-report] Stored report for {target_date}")


def generate_pdf_report(html_content, target_date):
    """Convert HTML email report to PDF using weasyprint.

    Returns the file path on success, None on failure.
    """
    try:
        from weasyprint import HTML
    except ImportError:
        print("[pdf] weasyprint not installed — skipping PDF generation")
        return None

    # PDF-specific CSS overrides — aggressive reset that does not rely on
    # matching inline style attribute values (which vary by Claude generation).
    # Uses universal selectors and element-type targeting instead.
    pdf_css = """
    <style>
        @page {
            size: A4;
            margin: 15mm;
        }

        /* ── Global reset ─────────────────────────────────────────── */
        body {
            font-family: Helvetica, Arial, sans-serif;
            font-size: 9px !important;
            line-height: 1.4 !important;
            color: #333;
            max-width: none;
            margin: 0;
            padding: 0;
        }
        /* Force every element to inherit the compact size */
        body * {
            font-size: inherit !important;
        }

        /* ── Headings ─────────────────────────────────────────────── */
        h1 {
            font-size: 13px !important;
            margin-top: 10px !important;
            margin-bottom: 4px !important;
        }
        h2, h3 {
            font-size: 11px !important;
            font-weight: bold !important;
            margin-top: 14px !important;
            margin-bottom: 6px !important;
        }

        /* ── Body text ────────────────────────────────────────────── */
        p, li, span, div {
            font-size: 9px !important;
            line-height: 1.4 !important;
        }

        /* ── Tables (Scorecard, Dominant Themes) ──────────────────── */
        table { font-size: 8px !important; width: 100% !important; }
        td, th {
            font-size: 8px !important;
            padding: 2px 6px !important;
        }

        /* ── Key Takeaway box (dark background near top) ──────────── */
        body > div:first-child + div,
        body > div:nth-child(2) {
            font-size: 10px !important;
            padding: 10px 14px !important;
        }

        /* ── Post cards ───────────────────────────────────────────── */
        /* Target any div with border that contains post content.
           Claude generates these as divs with background + border. */
        div > div > div {
            page-break-inside: avoid;
        }
        /* Bold text inside nested divs (post card headers) */
        div > div > div b,
        div > div > div strong {
            font-size: 8px !important;
            margin-bottom: 2px !important;
            display: block;
        }
        /* Italic text (analyst notes) */
        em, i {
            font-size: 8px !important;
        }
        /* Links (View on X) */
        a {
            font-size: 7px !important;
        }

        /* ── Aggressive post card size override ───────────────────── */
        /* Any div that is 3+ levels deep is likely a card or box.
           Shrink everything inside it. */
        div div div {
            padding: 6px !important;
            margin-bottom: 4px !important;
            font-size: 8px !important;
            line-height: 1.3 !important;
        }
        div div div * {
            font-size: 8px !important;
            line-height: 1.3 !important;
        }
        div div div span {
            font-size: 7px !important;
            color: #666 !important;
        }
        div div div a {
            font-size: 7px !important;
        }

        /* ── Scoring system / methodology (bottom boxes) ──────────── */
        body > div:last-child {
            font-size: 8px !important;
            padding: 8px 12px !important;
        }
        body > div:last-child * {
            font-size: 8px !important;
        }
    </style>
    """

    full_html = (
        '<!DOCTYPE html><html><head>'
        '<meta charset="utf-8">'
        f'{pdf_css}'
        f'</head><body>{html_content}</body></html>'
    )

    path = f"/tmp/kinetic_sentiment_{target_date}.pdf"
    try:
        HTML(string=full_html).write_pdf(path)
        print(f"[pdf] Generated {path}")
        return path
    except Exception as e:
        print(f"[pdf] Generation failed: {e}")
        return None


# ── distribution list ─────────────────────────────────────────────────────

def get_distribution_list(conn):
    """Return list of active recipients from x_distribution_list.

    Falls back to EMAIL_TO env var if the table is empty.
    Seeds the table from EMAIL_TO on first run.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT email, name FROM x_distribution_list WHERE active = TRUE "
        "ORDER BY added_at"
    )
    recipients = [{"email": r[0], "name": r[1] or ""} for r in cur.fetchall()]
    cur.close()

    if recipients:
        return recipients

    # Table is empty — seed from EMAIL_TO if set
    fallback = os.getenv("EMAIL_TO")
    if not fallback:
        return []

    cur = conn.cursor()
    cur.execute("""
        INSERT INTO x_distribution_list (email, name, active)
        VALUES (%s, %s, TRUE)
        ON CONFLICT (email) DO NOTHING
    """, (fallback, ""))
    conn.commit()
    cur.close()
    print(f"[email] Seeded distribution list with {fallback}")
    return [{"email": fallback, "name": ""}]


# ── Slack PDF posting ─────────────────────────────────────────────────────

def post_pdf_to_slack(pdf_path, target_date):
    """Upload a PDF report to Slack via the new file upload API.

    Three-step flow:
      1. files.getUploadURLExternal — get a presigned upload URL
      2. POST the file bytes to that URL
      3. files.completeUploadExternal — finalize and share to channel
    """
    bot_token = os.getenv("SLACK_BOT_TOKEN")
    channel_id = os.getenv("SLACK_CHANNEL_ID")

    if not bot_token or not channel_id:
        print("[slack-pdf] SLACK_BOT_TOKEN / SLACK_CHANNEL_ID not set — skipping")
        return False

    if not pdf_path:
        print("[slack-pdf] No PDF path — skipping")
        return False

    date_str = str(target_date)
    filename = f"Kinetic_AI_Sentiment_{date_str}.pdf"
    headers = {"Authorization": f"Bearer {bot_token}"}

    try:
        file_size = os.path.getsize(pdf_path)

        # Step 1: get upload URL
        resp = requests.get(
            "https://slack.com/api/files.getUploadURLExternal",
            headers=headers,
            params={"filename": filename, "length": file_size},
            timeout=15,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"[slack-pdf] getUploadURLExternal failed: {data.get('error')}")
            return False

        upload_url = data["upload_url"]
        file_id = data["file_id"]

        # Step 2: upload file bytes to the presigned URL
        with open(pdf_path, "rb") as f:
            resp = requests.post(
                upload_url,
                files={"file": (filename, f, "application/pdf")},
                timeout=30,
            )
        if resp.status_code != 200:
            print(f"[slack-pdf] Upload failed: {resp.status_code} {resp.text[:200]}")
            return False

        # Step 3: finalize and share to channel
        resp = requests.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "files": [{"id": file_id, "title": filename}],
                "channel_id": channel_id,
                "initial_comment": f"Kinetic AI Developer Sentiment — {date_str}",
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("ok"):
            print(f"[slack-pdf] Posted {filename} to channel")
            return True
        else:
            print(f"[slack-pdf] completeUploadExternal failed: {data.get('error')}")
            return False

    except Exception as e:
        print(f"[slack-pdf] Failed: {e}")
        return False


# ── email notification (SMTP) ────────────────────────────────────────────────

def send_email_summary(conn, summaries, target_date):
    """Generate rich HTML report via Claude and send via SMTP."""
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    email_from = os.getenv("EMAIL_FROM")

    if not all([smtp_host, smtp_user, smtp_pass, email_from]):
        print("[email] SMTP not configured — skipping email")
        return False

    recipients = get_distribution_list(conn)
    if not recipients:
        print("[email] No recipients configured — skipping email")
        return False

    # Generate the report
    email_html = generate_email_report(conn, target_date, summaries)
    if not email_html:
        print("[email] Report generation failed — skipping email")
        return False

    # Store for future reference
    store_email_report(conn, target_date, email_html)

    # Generate PDF attachment
    pdf_path = generate_pdf_report(email_html, target_date)

    date_str = str(target_date)
    total = sum(s["post_count"] for s in summaries)
    to_addrs = [r["email"] for r in recipients]

    # Use mixed for attachment support, with alternative nested for body
    msg = MIMEMultipart("mixed")
    msg["From"] = email_from
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = f"Kinetic AI Developer Sentiment — {date_str}"

    # HTML + plain text body as alternative part
    body = MIMEMultipart("alternative")
    plain = (f"Kinetic AI Developer Sentiment — {date_str}\n"
             f"{total} posts analyzed across "
             f"{len(set(s['provider'] for s in summaries))} providers.\n\n"
             f"View the full HTML report in an HTML-capable email client.")
    body.attach(MIMEText(plain, "plain"))
    body.attach(MIMEText(email_html, "html"))
    msg.attach(body)

    # Attach PDF if generated
    if pdf_path:
        try:
            with open(pdf_path, "rb") as f:
                pdf_part = MIMEBase("application", "pdf")
                pdf_part.set_payload(f.read())
            encoders.encode_base64(pdf_part)
            filename = f"Kinetic_AI_Sentiment_{date_str}.pdf"
            pdf_part.add_header(
                "Content-Disposition", "attachment", filename=filename)
            msg.attach(pdf_part)
            print(f"[email] Attached PDF: {filename}")
        except Exception as e:
            print(f"[email] PDF attachment failed: {e} — sending without")

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f"[email] Sent to {len(to_addrs)} recipients: "
              f"{', '.join(to_addrs)}")
        post_pdf_to_slack(pdf_path, target_date)
        return True
    except Exception as e:
        print(f"[email] Failed: {e}")
        return False


# ── main pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(test_mode=False, backfill_only=False, force_refresh_accounts=False):
    """Run the full X sentiment pipeline.

    Args:
        test_mode: Limit to 5 posts per provider for validation.
        backfill_only: Skip fetching, just rebuild summaries from existing data.
        force_refresh_accounts: Force re-discovery of tracked accounts.
    """
    start = time.time()
    today = datetime.now(ZoneInfo("America/New_York")).date()

    print(f"[pipeline] Starting Kinetic X Sentiment Tracker — {today}")
    if test_mode:
        print("[pipeline] TEST MODE — 5 posts per provider")

    ensure_tables()
    conn = get_db_connection()

    bearer = os.getenv("X_BEARER_TOKEN")
    if not bearer and not backfill_only:
        print("[pipeline] ERROR: X_BEARER_TOKEN not set")
        conn.close()
        return []

    fetch_failed = False

    if not backfill_only:
        try:
            # ── incremental: get last run timestamp ───────────────────────
            last_run = get_last_run(conn)
            if last_run:
                since = last_run
                print(f"[pipeline] Incremental fetch since {since.isoformat()}")
            else:
                since = datetime.now(timezone.utc) - timedelta(days=1)
                print(f"[pipeline] First run — fetching last 24h")

            # ── tracked accounts: refresh if needed ───────────────────────
            if force_refresh_accounts or _needs_account_refresh(conn):
                discover_tracked_accounts(conn, bearer, test_mode=test_mode)

            # ── load tracked account IDs for filter leniency ─────────────
            cur = conn.cursor()
            cur.execute("SELECT account_id FROM x_tracked_accounts")
            tracked_account_ids = {r[0] for r in cur.fetchall()}
            cur.close()

            # ── phase 1: tracked account posts (no engagement filter) ─────
            tracked_posts = search_tracked_account_posts(
                bearer, conn, since=since, test_mode=test_mode)
            tracked_posts = filter_developer_posts(
                tracked_posts, test_mode, tracked_account_ids)
            tracked_posts = deduplicate_posts(conn, tracked_posts)

            # ── phase 2: keyword search (with engagement filter) ──────────
            keyword_posts = []
            for provider, info in PROVIDERS.items():
                print(f"\n--- {provider} (keyword search) ---")
                posts = search_keyword_posts(
                    bearer, provider, info, since=since,
                    max_results=100, test_mode=test_mode)
                posts = filter_developer_posts(
                    posts, test_mode, tracked_account_ids)
                keyword_posts.extend(posts)
                time.sleep(1)

            keyword_posts = deduplicate_posts(conn, keyword_posts)

            # Review new accounts from keyword results for future tracking
            if keyword_posts and not test_mode:
                review_new_accounts(conn, keyword_posts)

            # ── merge and cap in test mode ────────────────────────────────
            tracked_ids = {p["post_id"] for p in tracked_posts}
            keyword_only = [p for p in keyword_posts
                            if p["post_id"] not in tracked_ids]
            all_new_posts = tracked_posts + keyword_only

            if test_mode:
                by_provider = defaultdict(list)
                for p in all_new_posts:
                    by_provider[p["provider"]].append(p)
                capped = []
                for posts in by_provider.values():
                    capped.extend(posts[:5])
                all_new_posts = capped

            print(f"\n[pipeline] Total new posts: {len(all_new_posts)} "
                  f"({len(tracked_posts)} tracked, {len(keyword_only)} keyword)")

            # ── store, analyze, store sentiment ───────────────────────────
            if all_new_posts:
                store_posts(conn, all_new_posts)

                by_provider = defaultdict(list)
                for p in all_new_posts:
                    by_provider[p["provider"]].append(p)

                total_analyzed = 0
                for provider, posts in by_provider.items():
                    analyses = analyze_posts_with_claude(posts, provider)
                    store_sentiments(conn, analyses)
                    total_analyzed += len(analyses)

                print(f"[pipeline] Analyzed {total_analyzed} posts total")

            # ── update last run timestamp (only on full success) ──────────
            update_last_run(conn)

        except Exception as e:
            print(f"\n[pipeline] FAILED: {e}")
            fetch_failed = True

    # ── build daily summary ───────────────────────────────────────────────
    # Still summarize whatever data we have, even after partial failure
    summaries = build_daily_summary(conn, today)

    if summaries:
        message = build_slack_message(summaries, today)
        print("\n" + message)
        post_to_slack(message)
        send_email_summary(conn, summaries, today)
    else:
        print("[pipeline] No data to summarize")

    conn.close()

    elapsed = time.time() - start
    print(f"\n[pipeline] Complete in {elapsed:.1f}s")

    if fetch_failed:
        return None

    return summaries


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="X Developer Sentiment Tracker")
    parser.add_argument("--test", action="store_true",
                        help="Test mode: 5 posts per provider")
    parser.add_argument("--backfill", action="store_true",
                        help="Rebuild summaries from existing data (no fetching)")
    parser.add_argument("--refresh-accounts", action="store_true",
                        help="Force refresh of tracked account list")
    parser.add_argument("--date", type=str, default=None,
                        help="Target date for summary (YYYY-MM-DD)")
    add_credentials_args(parser)
    args = parser.parse_args()

    load_credentials(secret_name=args.secrets, region=args.region)

    if args.date:
        os.environ["_X_TRACKER_DATE"] = args.date

    result = run_pipeline(
        test_mode=args.test,
        backfill_only=args.backfill,
        force_refresh_accounts=args.refresh_accounts,
    )
    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
