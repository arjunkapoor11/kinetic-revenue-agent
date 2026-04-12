import anthropic, os, sys, time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

AGENT_ID = "agent_011CZxfcRJtjRQRGnMDfJGEW"
CHECKPOINT_FILE = Path(__file__).parent / "pipeline_checkpoint.txt"

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Resolve environment ID — pass via ENV or ENVIRONMENT_ID in .env
environment_id = os.getenv("ENVIRONMENT_ID")
if not environment_id:
    print("No ENVIRONMENT_ID set — creating a new environment...")
    env = client.beta.environments.create(
        name="revenue-session-env",
        metadata={"project": "kinetic-revenue-agent"},
    )
    environment_id = env.id
    print(f"Environment: {environment_id}")

# Create session
session = client.beta.sessions.create(
    agent=AGENT_ID,
    environment_id=environment_id,
    title="Full pipeline — 10 tickers (test)",
)
print(f"Session: {session.id}")

TICKERS = ["SNOW", "DDOG", "MDB", "CRWD", "NOW", "PANW", "ZS", "PLTR", "APP", "HUBS"]
TICKERS_JSON = str(TICKERS)


# ── Checkpoint system ─────────────────────────────────────────────────────

def load_checkpoint():
    if not CHECKPOINT_FILE.exists():
        return set()
    completed = set()
    for line in CHECKPOINT_FILE.read_text().strip().splitlines():
        line = line.strip()
        if line:
            completed.add(line)
    return completed


def save_checkpoint(step_ticker):
    with open(CHECKPOINT_FILE, "a") as f:
        f.write(step_ticker + "\n")
        f.flush()
        os.fsync(f.fileno())
    print(f"[checkpoint] Saved: {step_ticker}")


def clear_checkpoint():
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()


completed = load_checkpoint()
if completed:
    print(f"[checkpoint] Resuming — {len(completed)} steps already completed")
else:
    clear_checkpoint()


# ── Send and wait with retry ─────────────────────────────────────────────

def send_and_wait(message_text, max_retries=2):
    """Send a message, stream until session goes idle. Retries on transient errors."""
    for attempt in range(1, max_retries + 2):
        try:
            with client.beta.sessions.events.stream(session.id) as stream:
                client.beta.sessions.events.send(
                    session.id,
                    events=[{
                        "type": "user.message",
                        "content": [{"type": "text", "text": message_text}],
                    }],
                )

                for event in stream:
                    match event.type:
                        case "agent.message":
                            for block in event.content:
                                if hasattr(block, "text"):
                                    print(block.text, end="", flush=True)
                        case "agent.tool_use":
                            print(f"\n[tool] {event.name}({event.input})")
                        case "session.status_idle":
                            return True
        except Exception as e:
            if attempt <= max_retries:
                print(f"\n[WARN] Attempt {attempt} failed: {type(e).__name__}: {e}")
                print(f"[WARN] Retrying in 5 seconds... ({max_retries - attempt + 1} retries left)")
                time.sleep(5)
            else:
                print(f"\n[ERROR] All {max_retries + 1} attempts failed: {type(e).__name__}: {e}")
                print(f"[ERROR] Skipping this message and continuing.")
                return False
    return False


# ── Pipeline steps ────────────────────────────────────────────────────────

# Step 1 — ingest_data
if "step1:ingest_data" not in completed:
    print(f"\n{'='*60}")
    print(f"  Step 1: ingest_data")
    print(f"{'='*60}")
    send_and_wait(f"Run ingest_data with tickers={TICKERS_JSON}.")
    save_checkpoint("step1:ingest_data")
else:
    print("[checkpoint] Skipping step 1 — already completed")

# Step 2 — ingest_transcripts
if "step2:ingest_transcripts" not in completed:
    print(f"\n{'='*60}")
    print(f"  Step 2: ingest_transcripts")
    print(f"{'='*60}")
    send_and_wait(f"Run ingest_transcripts with tickers={TICKERS_JSON}.")
    save_checkpoint("step2:ingest_transcripts")
else:
    print("[checkpoint] Skipping step 2 — already completed")

# Step 3 — analyze_transcripts, one ticker at a time
for i, ticker in enumerate(TICKERS, 1):
    key = f"step3:{ticker}"
    if key in completed:
        continue
    print(f"\n{'='*60}")
    print(f"  Step 3: [{i}/{len(TICKERS)}] analyze_transcripts for {ticker}")
    print(f"{'='*60}")
    try:
        send_and_wait(
            f'Run analyze_transcripts for ticker="{ticker}" only. '
            f"Nothing else — just this one ticker."
        )
        save_checkpoint(key)
    except KeyboardInterrupt:
        print(f"\n[ABORT] Interrupted during step 3 {ticker}. Resume with: python run_session_test.py")
        sys.exit(1)
    except BaseException as e:
        print(f"\n[ERROR] Step 3 {ticker} failed: {type(e).__name__}: {e}", flush=True)
        print(f"[ERROR] Continuing to next ticker.", flush=True)
        time.sleep(2)

# Step 4 — run_analysis, one ticker at a time
for i, ticker in enumerate(TICKERS, 1):
    key = f"step4:{ticker}"
    if key in completed:
        continue
    print(f"\n{'='*60}")
    print(f"  Step 4: [{i}/{len(TICKERS)}] run_analysis for {ticker}")
    print(f"{'='*60}")
    try:
        send_and_wait(
            f'Run run_analysis for ticker="{ticker}" only. '
            f"Nothing else — just this one ticker."
        )
        save_checkpoint(key)
    except KeyboardInterrupt:
        print(f"\n[ABORT] Interrupted during step 4 {ticker}. Resume with: python run_session_test.py")
        sys.exit(1)
    except BaseException as e:
        print(f"\n[ERROR] Step 4 {ticker} failed: {type(e).__name__}: {e}", flush=True)
        print(f"[ERROR] Continuing to next ticker.", flush=True)
        time.sleep(2)

# Step 5 — generate_dashboard
if "step5:generate_dashboard" not in completed:
    print(f"\n{'='*60}")
    print(f"  Step 5: generate_dashboard")
    print(f"{'='*60}")
    send_and_wait("Run generate_dashboard now.")
    save_checkpoint("step5:generate_dashboard")
else:
    print("[checkpoint] Skipping step 5 — already completed")

# Step 6 — export_to_excel
if "step6:export_to_excel" not in completed:
    print(f"\n{'='*60}")
    print(f"  Step 6: export_to_excel")
    print(f"{'='*60}")
    send_and_wait("Run export_to_excel now.")
    save_checkpoint("step6:export_to_excel")
else:
    print("[checkpoint] Skipping step 6 — already completed")

# Step 7 — post_to_slack
if "step7:post_to_slack" not in completed:
    print(f"\n{'='*60}")
    print(f"  Step 7: post_to_slack")
    print(f"{'='*60}")
    send_and_wait("Run post_to_slack now.")
    save_checkpoint("step7:post_to_slack")
else:
    print("[checkpoint] Skipping step 7 — already completed")

# Clean up checkpoint on successful completion
clear_checkpoint()

print(f"\n{'='*60}")
print(f"  Pipeline complete — {len(TICKERS)} tickers, all 7 steps.")
print(f"{'='*60}")
