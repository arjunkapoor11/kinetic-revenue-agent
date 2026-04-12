# Deployment

Covers `mcp_server.py`, `deploy_agent.py`, and the AWS production architecture for running the Kinetic Revenue Agent as a Claude Managed Agent.

## Architecture

```
Anthropic Console
  └── Managed Agent (claude-sonnet-4-6)
        ├── System prompt (loaded from skills/*.md via deploy_agent.py)
        ├── Built-in tools (agent_toolset_20260401)
        └── MCP Server connection (HTTPS/SSE)
                │
                ▼
AWS EC2 (us-east-2, same AZ as RDS)
  └── mcp_server.py (FastMCP, SSE transport, port 3001)
        ├── ingest_data        → ingest.py
        ├── ingest_transcripts → transcript_ingest.py
        ├── analyze_transcripts → transcript_analyzer.py
        ├── run_analysis       → agent.py
        ├── generate_dashboard → dashboard.py
        └── export_to_excel    → export.py
                │
                ▼
AWS Secrets Manager          AWS RDS PostgreSQL
  kinetic-revenue-agent        revenue-agent-db
  (DB_HOST, DB_PASSWORD,       (revenue_actuals,
   FMP_API_KEY, etc.)           consensus_estimates,
                                agent_reports,
                                transcripts)
```

## Components

### `mcp_server.py` — MCP Server

A FastMCP server that exposes the 6 pipeline tools over HTTP/SSE. The Managed Agent connects to this server via its public URL.

**Key features:**
- **SSE transport** — standard MCP-over-HTTP protocol for remote tool execution
- **Dual credential loading** — AWS Secrets Manager in production, `.env` for local dev
- **Tool isolation** — each tool imports from the pipeline scripts and runs in a captured context (stdout/stderr captured, exceptions caught)
- **No state between calls** — each tool invocation imports fresh modules and creates new DB connections

**Local development:**
```bash
python mcp_server.py
# Starts on http://localhost:3001, reads from .env
```

**Production:**
```bash
python mcp_server.py --secrets kinetic-revenue-agent --region us-east-2 --host 0.0.0.0 --port 3001
# Reads credentials from AWS Secrets Manager, binds to all interfaces
```

**Endpoints exposed:**
| Path | Protocol | Purpose |
|------|----------|---------|
| `/sse` | SSE | MCP-over-SSE (primary connection point for Managed Agents) |
| `/mcp` | HTTP | Streamable HTTP transport (alternative) |
| `/messages/` | HTTP | Message endpoint |

### `deploy_agent.py` — Deployment Script

Registers the agent in the Anthropic Console using the Managed Agents beta API (`managed-agents-2026-04-01`).

**What it creates:**
1. **Agent** — model, system prompt (from skills files), MCP server URL, tool configuration
2. **Vault** — encrypted credential storage for DB and API keys
3. **Environment** — cloud container configuration with networking

**Usage:**
```bash
# First deployment
python deploy_agent.py --mcp-url https://your-ec2:3001/sse

# Update existing agent
python deploy_agent.py --mcp-url https://your-ec2:3001/sse --agent-id ag_xxxx

# Dry run (no API calls)
python deploy_agent.py --mcp-url https://your-ec2:3001/sse --dry-run

# Skip vault/env creation (use existing)
python deploy_agent.py --mcp-url https://your-ec2:3001/sse --skip-vault --skip-env
```

**CLI flags:**
| Flag | Purpose |
|------|---------|
| `--mcp-url` | Public HTTPS URL of the MCP server (required) |
| `--agent-id` | Existing agent ID to update (omit to create new) |
| `--credentials` | Path to JSON file with credentials (omit for .env) |
| `--dry-run` | Print config without calling API |
| `--skip-vault` | Don't create vault |
| `--skip-env` | Don't create environment |

## Credential Vaulting

**Principle: credentials never leave AWS.**

### Production (AWS Secrets Manager)
The MCP server reads all credentials from a single Secrets Manager secret at startup:

```json
{
  "DB_HOST": "revenue-agent-db.ctk68qkkavr2.us-east-2.rds.amazonaws.com",
  "DB_NAME": "postgres",
  "DB_USER": "postgres",
  "DB_PASSWORD": "...",
  "FMP_API_KEY": "...",
  "ANTHROPIC_API_KEY": "...",
  "RAPIDAPI_KEY": "..."
}
```

The MCP server sets these as environment variables on startup so the pipeline scripts (which use `os.getenv()`) work without modification.

### Anthropic Vault
The deploy script also creates a vault in the Anthropic Console. This makes credentials available to the Managed Agent's cloud environment if the agent needs to run tools outside the MCP server.

### Local Development
For local development, credentials come from `.env` as usual. The MCP server detects whether `--secrets` was passed and loads accordingly.

### Creating the Secrets Manager secret:
```bash
aws secretsmanager create-secret \
  --name kinetic-revenue-agent \
  --region us-east-2 \
  --secret-string '{
    "DB_HOST": "revenue-agent-db.ctk68qkkavr2.us-east-2.rds.amazonaws.com",
    "DB_NAME": "postgres",
    "DB_USER": "postgres",
    "DB_PASSWORD": "...",
    "FMP_API_KEY": "...",
    "ANTHROPIC_API_KEY": "...",
    "RAPIDAPI_KEY": "..."
  }'
```

## AWS Deployment — EC2

### Why EC2 over Lambda
- Pipeline steps are long-running (step 4 takes 60-90 seconds for 5 tickers)
- Lambda has a 15-minute timeout and cold start overhead
- EC2 keeps the MCP server warm and colocated with RDS
- The SSE connection is persistent — Lambda would need API Gateway WebSocket

### Setup Steps

**1. Launch EC2 instance:**
```bash
# t3.medium is sufficient (2 vCPU, 4GB RAM)
# Use Amazon Linux 2023 AMI
# Place in same VPC and subnet as RDS
# Security group: allow inbound 3001 from Anthropic IP ranges, allow outbound to RDS
```

**2. Install dependencies:**
```bash
sudo dnf install python3.13 python3.13-pip git -y
git clone <your-repo> /opt/kinetic-revenue-agent
cd /opt/kinetic-revenue-agent
pip3.13 install -r requirements.txt
pip3.13 install boto3
```

**3. Create requirements.txt:**
```
anthropic>=0.94.0
psycopg2-binary>=2.9
requests>=2.31
python-dotenv>=1.0
openpyxl>=3.1
mcp>=1.27
uvicorn>=0.44
starlette>=1.0
boto3>=1.34
```

**4. Configure systemd service:**
```ini
# /etc/systemd/system/kinetic-mcp.service
[Unit]
Description=Kinetic Revenue Agent MCP Server
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/opt/kinetic-revenue-agent
ExecStart=/usr/bin/python3.13 mcp_server.py --secrets kinetic-revenue-agent --region us-east-2 --host 0.0.0.0 --port 3001
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable kinetic-mcp
sudo systemctl start kinetic-mcp
```

**5. HTTPS with Caddy (reverse proxy):**
```bash
# Install Caddy
sudo dnf install caddy -y

# /etc/caddy/Caddyfile
your-domain.com {
    reverse_proxy localhost:3001
}

sudo systemctl enable caddy
sudo systemctl start caddy
```

This gives you a public HTTPS endpoint with automatic TLS certificates.

**6. IAM role for Secrets Manager access:**
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetSecretValue"
            ],
            "Resource": "arn:aws:secretsmanager:us-east-2:*:secret:kinetic-revenue-agent-*"
        }
    ]
}
```

Attach this policy to the EC2 instance's IAM role.

### Security

| Layer | Control |
|-------|---------|
| Network | EC2 security group restricts inbound to Anthropic IPs + your IP |
| Transport | HTTPS via Caddy with automatic TLS |
| Credentials | AWS Secrets Manager, never on disk |
| Database | RDS security group allows only EC2 security group |
| IAM | Least-privilege role: only `secretsmanager:GetSecretValue` for one secret |

## Deployment Workflow

### First-time deployment:
```bash
# 1. Create Secrets Manager secret
aws secretsmanager create-secret --name kinetic-revenue-agent --region us-east-2 --secret-string '...'

# 2. Launch EC2, install dependencies, start MCP server
ssh ec2-user@your-ec2 "cd /opt/kinetic-revenue-agent && python3.13 mcp_server.py --secrets kinetic-revenue-agent"

# 3. Register agent in Anthropic Console
python deploy_agent.py --mcp-url https://your-domain.com/sse

# 4. Note the agent ID from the output
```

### Updating the agent:
```bash
# Update code on EC2
ssh ec2-user@your-ec2 "cd /opt/kinetic-revenue-agent && git pull && sudo systemctl restart kinetic-mcp"

# Update agent config in Anthropic Console
python deploy_agent.py --mcp-url https://your-domain.com/sse --agent-id ag_xxxx --skip-vault --skip-env
```

### Running the agent:
```bash
# Via Anthropic Console UI: open the agent and type a prompt
# Via API:
python -c "
import anthropic
client = anthropic.Anthropic()
session = client.beta.sessions.create(agent_id='ag_xxxx', environment_id='env_xxxx')
client.beta.sessions.events.create(session_id=session.id, events=[{
    'type': 'user.message',
    'content': [{'type': 'text', 'text': 'Run the full pipeline for SNOW and DDOG'}]
}])
# Stream results...
"
```

## Alternative: AWS Lambda + API Gateway

If pipeline steps were shorter (<15 min), Lambda would be viable:

```
API Gateway (WebSocket)
  └── Lambda function
        └── mcp_server handler
              └── Pipeline tools
                    └── RDS (via VPC Lambda)
```

**Pros:** no server to manage, scales to zero, pay-per-use
**Cons:** 15-min timeout (step 4 can take 90s × 5 tickers = 7.5 min), cold starts add 5-10s, WebSocket complexity

**Verdict:** EC2 is the right choice for this pipeline's runtime characteristics.

## Managed Agents API Reference

All API calls use the beta header: `anthropic-beta: managed-agents-2026-04-01`

| Operation | Endpoint | Script Function |
|-----------|----------|-----------------|
| Create agent | `POST /v1/agents` | `deploy_agent.create_agent()` |
| Update agent | `PATCH /v1/agents/{id}` | `deploy_agent.update_agent()` |
| Create vault | `POST /v1/vaults` | `deploy_agent.create_vault()` |
| Create environment | `POST /v1/environments` | `deploy_agent.create_environment()` |
| Create session | `POST /v1/sessions` | `deploy_agent.create_session()` |

## File Inventory

| File | Purpose |
|------|---------|
| `mcp_server.py` | FastMCP server exposing 6 pipeline tools over SSE |
| `deploy_agent.py` | Registers agent + vault + environment in Anthropic Console |
| `managed_agent.py` | Local orchestrator (Anthropic API tool-use loop, for dev/testing) |
| `skills/deployment.md` | This documentation |
