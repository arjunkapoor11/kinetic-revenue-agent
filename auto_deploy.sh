#!/bin/bash
# Kinetic Revenue Agent — Auto-Deploy
#
# Pulls latest code from origin/main every 2 minutes.
# Restarts the MCP server if any Python files changed.
#
# Install: crontab -e, then add:
#   */2 * * * * /home/ec2-user/revenue-agent/auto_deploy.sh

WORKDIR=/home/ec2-user
LOGFILE=/home/ec2-user/logs/deploy.log
PYTHON=python3.11
MCP_PORT=3001

mkdir -p "$(dirname "$LOGFILE")"

cd "$WORKDIR" || exit 1

# Capture current HEAD before pull
BEFORE=$(git rev-parse HEAD)

# Pull latest — quiet unless there are changes
git pull --ff-only origin main >> "$LOGFILE" 2>&1
PULL_EXIT=$?

if [ $PULL_EXIT -ne 0 ]; then
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') [ERROR] git pull failed (exit $PULL_EXIT)" >> "$LOGFILE"
    exit 1
fi

AFTER=$(git rev-parse HEAD)

# Nothing changed — exit silently
if [ "$BEFORE" = "$AFTER" ]; then
    exit 0
fi

# Something changed — log what
CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")
echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') [DEPLOY] $BEFORE -> $AFTER" >> "$LOGFILE"
echo "$CHANGED" >> "$LOGFILE"

# Check if any Python files changed
PY_CHANGED=$(echo "$CHANGED" | grep '\.py$' || true)

if [ -n "$PY_CHANGED" ]; then
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') [RESTART] Python files changed — restarting MCP server" >> "$LOGFILE"

    # Kill existing MCP server
    MCP_PID=$(lsof -ti :"$MCP_PORT" 2>/dev/null || true)
    if [ -n "$MCP_PID" ]; then
        kill "$MCP_PID" 2>/dev/null
        sleep 2
        # Force kill if still running
        kill -9 "$MCP_PID" 2>/dev/null || true
    fi

    # Start MCP server in background
    nohup $PYTHON mcp_server.py --secrets kinetic-revenue-agent >> /home/ec2-user/logs/mcp_server.log 2>&1 &
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') [RESTART] MCP server started (PID $!)" >> "$LOGFILE"
else
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') [DEPLOY] No Python files changed — skipping restart" >> "$LOGFILE"
fi
