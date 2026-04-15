"""
Kinetic Revenue Agent — Deployment Script

Registers the agent in the Anthropic Console using the Managed Agents beta API.
Creates the agent, vault, environment, and connects the MCP server.

Usage:
    # Deploy with MCP server URL
    python deploy_agent.py --mcp-url https://your-ec2:3001/sse

    # Update an existing agent
    python deploy_agent.py --mcp-url https://your-ec2:3001/sse --agent-id ag_xxx

    # Deploy with vault credentials from a JSON file
    python deploy_agent.py --mcp-url https://your-ec2:3001/sse --credentials creds.json

    # Dry run (print what would be created, don't call API)
    python deploy_agent.py --mcp-url https://your-ec2:3001/sse --dry-run

Required: ANTHROPIC_API_KEY environment variable or .env file.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from credentials import load_credentials, add_credentials_args

load_dotenv()

PROJECT_DIR = Path(__file__).parent
BETA_HEADER = "managed-agents-2026-04-01"


# ── system prompt builder ──────────────────────────────────────────────────

def build_system_prompt() -> str:
    """Build the managed agent system prompt from skills files."""
    skills_dir = PROJECT_DIR / "skills"
    sections = []

    for md_file in sorted(skills_dir.glob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        sections.append(f"## {md_file.stem}\n\n{content}")

    skills_text = "\n\n---\n\n".join(sections)

    return f"""You are the Kinetic Revenue Agent — a quantitative revenue analysis system
for high-growth SaaS companies (SNOW, DDOG, MDB, TENB, QLYS).

You have access to 6 pipeline tools via the kinetic_pipeline MCP server.
Execute them in this exact order when asked to run the pipeline:

1. ingest_data — Pull revenue actuals + consensus from FMP API into PostgreSQL
2. ingest_transcripts — Fetch earnings call transcripts from Seeking Alpha
3. analyze_transcripts — Claude analyzes transcripts for anomalous quarters
4. run_analysis — Full $ QoQ analysis + Claude research notes
5. generate_dashboard — Write interactive HTML dashboard
6. export_to_excel — Write professional Excel financial model

EXECUTION RULES:
- Steps must run in order. Each depends on the previous step's output.
- After each tool call, check success/failure in the result.
- If a step FAILS, retry it once. If the retry also fails, STOP and report.
- Pass the tickers array to steps 1-4. Steps 5-6 take no arguments.
- After completion, provide a summary table: Step | Tool | Status.

METHODOLOGY:
- $ QoQ (dollar quarter-over-quarter) is the primary forecasting metric
- Beat cadence measures historical actual vs consensus divergence
- Guide inference projects what management will guide to
- See the skills reference below for full methodology documentation.

SKILLS REFERENCE:
{skills_text}
"""


# ── deployment functions ───────────────────────────────────────────────────

def create_agent(client: anthropic.Anthropic, mcp_url: str, dry_run: bool = False):
    """Create or register the managed agent in the Anthropic Console."""
    system_prompt = build_system_prompt()
    print(f"[agent] System prompt: {len(system_prompt):,} chars")

    agent_config = dict(
        model="claude-sonnet-4-6",
        name="Kinetic Revenue Agent",
        description=(
            "Quarterly revenue analysis system for high-growth SaaS companies. "
            "Ingests financial data from FMP, analyzes earnings transcripts, "
            "computes $ QoQ projections with beat cadence and guide inference, "
            "and generates dashboards and Excel models."
        ),
        system=system_prompt,
        mcp_servers=[
            {
                "type": "url",
                "name": "kinetic_pipeline",
                "url": mcp_url,
            }
        ],
        tools=[
            {"type": "agent_toolset_20260401"},
            {"type": "mcp_toolset", "mcp_server_name": "kinetic_pipeline"},
        ],
    )

    if dry_run:
        print("[dry-run] Would create agent with config:")
        safe = {k: v for k, v in agent_config.items() if k != "system"}
        safe["system"] = f"({len(system_prompt):,} chars)"
        print(json.dumps(safe, indent=2, default=str))
        return None

    agent = client.beta.agents.create(**agent_config)
    print(f"[agent] Created: {agent.id} (version {agent.version})")
    return agent


def update_agent(client: anthropic.Anthropic, agent_id: str, mcp_url: str):
    """Update an existing agent's system prompt and MCP server."""
    system_prompt = build_system_prompt()

    # Retrieve current version for optimistic locking
    current = client.beta.agents.retrieve(agent_id)

    agent = client.beta.agents.update(
        agent_id,
        version=current.version,
        model="claude-sonnet-4-6",
        name="Kinetic Revenue Agent",
        system=system_prompt,
        mcp_servers=[
            {
                "type": "url",
                "name": "kinetic_pipeline",
                "url": mcp_url,
            }
        ],
        tools=[
            {"type": "agent_toolset_20260401"},
            {"type": "mcp_toolset", "mcp_server_name": "kinetic_pipeline"},
        ],
    )
    print(f"[agent] Updated: {agent.id} (version {agent.version})")
    return agent


def create_vault(client: anthropic.Anthropic, creds_path: str | None,
                 dry_run: bool = False):
    """Create a vault with DB and API credentials.

    If creds_path is provided, reads from JSON file. Otherwise reads from
    environment / .env file.
    """
    if creds_path:
        with open(creds_path) as f:
            creds = json.load(f)
    else:
        creds = {
            "DB_HOST": os.getenv("DB_HOST", ""),
            "DB_NAME": os.getenv("DB_NAME", ""),
            "DB_USER": os.getenv("DB_USER", ""),
            "DB_PASSWORD": os.getenv("DB_PASSWORD", ""),
            "FMP_API_KEY": os.getenv("FMP_API_KEY", ""),
            "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
            "RAPIDAPI_KEY": os.getenv("RAPIDAPI_KEY", ""),
        }

    if dry_run:
        print(f"[dry-run] Would create vault with {len(creds)} credentials: "
              f"{', '.join(creds.keys())}")
        return None

    vault = client.beta.vaults.create(
        name="kinetic-revenue-credentials",
        metadata={"project": "kinetic-revenue-agent"},
    )

    for key, value in creds.items():
        if value:
            client.beta.vaults.credentials.create(
                vault_id=vault.id,
                name=key,
                value=value,
            )

    print(f"[vault] Created: {vault.id} ({len(creds)} credentials)")
    return vault


def create_environment(client: anthropic.Anthropic, dry_run: bool = False):
    """Create a cloud environment for the agent."""
    if dry_run:
        print("[dry-run] Would create cloud environment with unrestricted networking")
        return None

    environment = client.beta.environments.create(
        name="kinetic-revenue-env",
        metadata={"project": "kinetic-revenue-agent"},
    )
    print(f"[environment] Created: {environment.id}")
    return environment


def create_session(client: anthropic.Anthropic, agent_id: str,
                   environment_id: str, vault_id: str | None = None):
    """Create a session to run the agent."""
    kwargs = dict(
        agent_id=agent_id,
        environment_id=environment_id,
    )
    if vault_id:
        kwargs["vault_ids"] = [vault_id]

    session = client.beta.sessions.create(**kwargs)
    print(f"[session] Created: {session.id}")
    return session


# ── main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Deploy Kinetic Revenue Agent to Anthropic Console")
    parser.add_argument("--mcp-url", required=True,
                        help="Public URL of the MCP server (e.g. https://host:3001/sse)")
    parser.add_argument("--agent-id", default=None,
                        help="Existing agent ID to update (omit to create new)")
    parser.add_argument("--credentials", default=None,
                        help="Path to credentials JSON file (omit to use .env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be created without calling API")
    parser.add_argument("--skip-vault", action="store_true",
                        help="Skip vault creation (use existing vault)")
    parser.add_argument("--skip-env", action="store_true",
                        help="Skip environment creation (use existing)")
    add_credentials_args(parser)
    args = parser.parse_args()

    load_credentials(secret_name=args.secrets, region=args.region)

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set. Add it to .env or export it.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    print("=" * 60)
    print("  Kinetic Revenue Agent — Deployment")
    print("=" * 60)
    print(f"  MCP Server: {args.mcp_url}")
    print(f"  Mode: {'Update' if args.agent_id else 'Create'}")
    print(f"  Dry Run: {args.dry_run}")
    print()

    # 1. Create or update agent
    if args.agent_id:
        agent = update_agent(client, args.agent_id, args.mcp_url)
    else:
        agent = create_agent(client, args.mcp_url, args.dry_run)

    # 2. Create vault (unless skipped)
    vault = None
    if not args.skip_vault:
        vault = create_vault(client, args.credentials, args.dry_run)

    # 3. Create environment (unless skipped)
    env = None
    if not args.skip_env:
        env = create_environment(client, args.dry_run)

    # 4. Summary
    print()
    print("=" * 60)
    print("  Deployment Summary")
    print("=" * 60)
    if not args.dry_run:
        print(f"  Agent ID:       {agent.id if agent else 'N/A'}")
        print(f"  Vault ID:       {vault.id if vault else 'skipped'}")
        print(f"  Environment ID: {env.id if env else 'skipped'}")
        print(f"  MCP Server:     {args.mcp_url}")
        print()
        print("  Next steps:")
        print("    1. Ensure MCP server is running at the URL above")
        print("    2. Create a session:")
        print(f"       python deploy_agent.py --create-session --agent-id {agent.id}")
        print("    3. Or use the agent in the Anthropic Console")
    else:
        print("  (dry run — nothing was created)")


if __name__ == "__main__":
    main()
