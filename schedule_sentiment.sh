#!/bin/bash
# Kinetic Revenue Agent — X Sentiment Tracker Schedule
#
# Runs the X developer sentiment pipeline. Credentials loaded from
# AWS Secrets Manager via --secrets flag (no .env sourcing needed).
#
# Install: crontab -e, then add:
#   30 21 * * 1-5 /home/ec2-user/revenue-agent/schedule_sentiment.sh
#   (21:30 UTC = 5:30 PM EST, weekdays only)

set -uo pipefail

WORKDIR=/home/ec2-user/revenue-agent
LOGDIR=/home/ec2-user/logs
LOGFILE="${LOGDIR}/sentiment_$(date +%Y%m%d).log"
PYTHON=python3.11

mkdir -p "$LOGDIR"

echo "========================================" >> "$LOGFILE"
echo "  X Sentiment Tracker started: $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOGFILE"
echo "========================================" >> "$LOGFILE"

cd "$WORKDIR"

$PYTHON x_sentiment_tracker.py --secrets kinetic-revenue-agent >> "$LOGFILE" 2>&1
EXIT_CODE=$?

echo "" >> "$LOGFILE"
if [ $EXIT_CODE -eq 0 ]; then
    echo "[COMPLETE] X Sentiment Tracker finished at $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOGFILE"
else
    echo "[FAILED] X Sentiment Tracker exited with code $EXIT_CODE at $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOGFILE"
fi

# Clean up logs older than 30 days
find "$LOGDIR" -name "sentiment_*.log" -mtime +30 -delete

exit $EXIT_CODE
