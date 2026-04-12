import anthropic, os, sys
from dotenv import load_dotenv
load_dotenv()

AGENT_ID = "agent_011CZxfcRJtjRQRGnMDfJGEW"

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
    title="Full pipeline — 49 tickers",
)
print(f"Session: {session.id}")

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


def send_and_wait(message_text):
    """Send a message, stream until session goes idle, return."""
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
                    return


# Step 4 — run_analysis, one ticker at a time
for i, ticker in enumerate(TICKERS, 1):
    print(f"\n{'='*60}")
    print(f"  [{i}/{len(TICKERS)}] run_analysis for {ticker}")
    print(f"{'='*60}")
    send_and_wait(
        f'Run run_analysis for ticker="{ticker}" only. '
        f"Nothing else — just this one ticker."
    )

# Step 5 — generate_dashboard
print(f"\n{'='*60}")
print(f"  generate_dashboard")
print(f"{'='*60}")
send_and_wait("Run generate_dashboard now.")

# Step 6 — export_to_excel
print(f"\n{'='*60}")
print(f"  export_to_excel")
print(f"{'='*60}")
send_and_wait("Run export_to_excel now.")

print(f"\n{'='*60}")
print(f"  Pipeline complete — {len(TICKERS)} tickers processed.")
print(f"{'='*60}")
