#!/bin/bash
# Manual on-demand trigger for the X developer sentiment pipeline.
# Same code path as schedule_sentiment.sh; output goes to stdout instead
# of a dated log file. Use this for ad-hoc runs and end-to-end tests.

set -uo pipefail

WORKDIR=/home/ec2-user
PYTHON=python3.11

cd "$WORKDIR"
exec $PYTHON x_sentiment_tracker.py --secrets kinetic-revenue-agent
