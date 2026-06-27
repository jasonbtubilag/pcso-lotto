#!/bin/bash
# Wrapper to run the PCSO updater on a schedule (cron/launchd).
# Logs every run to pcso_update.log in the same folder.

PROJECT_DIR="/Users/jasontubilag/Documents/Claude/Projects/Statistics and Probabilities"
LOG_FILE="$PROJECT_DIR/pcso_update.log"

# Make common Python install locations visible to cron's bare PATH.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

echo "==================================================" >> "$LOG_FILE"
echo "Run started: $(date)" >> "$LOG_FILE"

cd "$PROJECT_DIR" || exit 1

# Step 1: scrape latest results and rebuild CSV/JSON/HTML.
python3 "$PROJECT_DIR/pcso_update.py" >> "$LOG_FILE" 2>&1
UPDATE_EXIT=$?

# Step 2: re-inject predictions + track record into the HTML.
# (pcso_update.py rebuilds the HTML from scratch and removes these panels,
#  so the predict script must run AFTER it every time.)
python3 "$PROJECT_DIR/pcso_predict.py" >> "$LOG_FILE" 2>&1
PREDICT_EXIT=$?

echo "Run finished: $(date) (update exit $UPDATE_EXIT, predict exit $PREDICT_EXIT)" >> "$LOG_FILE"
