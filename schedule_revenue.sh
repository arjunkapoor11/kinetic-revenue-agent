#!/bin/bash
# Kinetic Revenue Agent — Revenue Pipeline Schedule
#
# Runs the full 48-ticker revenue pipeline via run_session.py.
# Credentials loaded via --secrets on scripts that support it;
# run_session.py uses ENVIRONMENT_ID from Secrets Manager env.
#
# Install: crontab -e, then add:
#   0 11 * * 1-5 /home/ec2-user/revenue-agent/schedule_revenue.sh
#   (11:00 UTC = 6:00 AM EST, weekdays only)

set -uo pipefail

WORKDIR=/home/ec2-user/revenue-agent
LOGDIR=/home/ec2-user/logs
LOGFILE="${LOGDIR}/revenue_$(date +%Y%m%d).log"
PYTHON=python3.11

mkdir -p "$LOGDIR"

echo "========================================" >> "$LOGFILE"
echo "  Revenue Pipeline started: $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOGFILE"
echo "========================================" >> "$LOGFILE"

cd "$WORKDIR"

# run_session.py does not yet support --secrets; it reads ENVIRONMENT_ID
# and ANTHROPIC_API_KEY from the EC2 instance environment (set via
# Secrets Manager env injection or userdata). No source .env needed
# when the EC2 environment is configured correctly.
$PYTHON run_session.py >> "$LOGFILE" 2>&1
EXIT_CODE=$?

echo "" >> "$LOGFILE"
if [ $EXIT_CODE -eq 0 ]; then
    echo "[COMPLETE] Revenue pipeline finished at $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOGFILE"
else
    echo "[FAILED] Revenue pipeline exited with code $EXIT_CODE at $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOGFILE"
fi

# Clean up logs older than 30 days
find "$LOGDIR" -name "revenue_*.log" -mtime +30 -delete

exit $EXIT_CODE
