#!/bin/bash
# Kinetic Revenue Agent — Scheduled Pipeline Run
# Runs full 48-ticker pipeline. Designed for cron on EC2.
#
# Install: crontab -e, then add:
#   0 11 * * 1-5 /home/ec2-user/revenue-agent/schedule.sh
#   (11:00 UTC = 6:00 AM EST, weekdays only)

set -euo pipefail

WORKDIR=/home/ec2-user/revenue-agent
LOGDIR=/home/ec2-user/logs
LOGFILE="${LOGDIR}/pipeline_$(date +%Y%m%d).log"
PYTHON=python3

# Ensure log directory exists
mkdir -p "$LOGDIR"

echo "========================================" >> "$LOGFILE"
echo "  Pipeline started: $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOGFILE"
echo "========================================" >> "$LOGFILE"

cd "$WORKDIR"

# Load environment variables
set -a
source .env
set +a

# Run the full pipeline
$PYTHON run_session.py >> "$LOGFILE" 2>&1
EXIT_CODE=$?

echo "" >> "$LOGFILE"
if [ $EXIT_CODE -eq 0 ]; then
    echo "[COMPLETE] Pipeline finished successfully at $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOGFILE"
else
    echo "[FAILED] Pipeline exited with code $EXIT_CODE at $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOGFILE"
fi

# ── X Sentiment Tracker (runs at 5:30pm ET / 21:30 UTC, weekdays) ────────
# Install separately: crontab -e, then add:
#   30 21 * * 1-5 /home/ec2-user/revenue-agent/schedule.sh --x-sentiment
#
if [ "${1:-}" = "--x-sentiment" ]; then
    echo "" >> "$LOGFILE"
    echo "========================================" >> "$LOGFILE"
    echo "  X Sentiment Tracker started: $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOGFILE"
    echo "========================================" >> "$LOGFILE"

    $PYTHON x_sentiment_tracker.py >> "$LOGFILE" 2>&1
    X_EXIT=$?

    if [ $X_EXIT -eq 0 ]; then
        echo "[COMPLETE] X Sentiment Tracker finished at $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOGFILE"
    else
        echo "[FAILED] X Sentiment Tracker exited with code $X_EXIT at $(date -u '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOGFILE"
    fi
fi

# Clean up logs older than 30 days
find "$LOGDIR" -name "pipeline_*.log" -mtime +30 -delete

exit $EXIT_CODE
