# PCSO Lotto Results — Auto-Updating Database & Dashboard

This package keeps a local database of PCSO lotto results for 5 games
(Ultra Lotto 6/58, Grand Lotto 6/55, Super Lotto 6/49, Mega Lotto 6/45,
Lotto 6/42) and regenerates a self-contained HTML dashboard.

## Files
- pcso_update.py            The updater script (scrapes PCSO, rebuilds everything)
- pcso_lotto_results.html   The dashboard (open in any browser, works offline)
- pcso_lotto_results.csv    Database in spreadsheet format
- pcso_lotto_results.json   Database in JSON format

The dashboard shows, at the top, **which games are drawn TODAY** (recalculated
each time you open it) plus the full weekly schedule, followed by a searchable,
sortable, filterable table of every draw.

## 1. Install Python dependencies (one time)
    pip install requests beautifulsoup4

## 2. Run the updater
    python pcso_update.py

- First run with no existing JSON: it scrapes the FULL history (2016 -> now).
- Later runs: it only pulls the last ~30 days and merges any new draws,
  then rewrites the CSV, JSON, and HTML.

> Tip: You already have a populated database from your initial extraction.
> Keep the .json file next to the script so the updater runs in fast
> incremental mode instead of re-scraping everything.

## 3. Schedule it to run every day at 10:00 PM

### macOS / Linux (cron)
Open your crontab:
    crontab -e
Add this line (adjust the path to where you saved the files):
    0 22 * * * /usr/bin/python3 /full/path/to/pcso_update.py >> /full/path/to/pcso_update.log 2>&1
- "0 22 * * *" = every day at 22:00 (10 PM).
- Find your python path with:  which python3

### macOS (launchd alternative)
If cron is restricted, create ~/Library/LaunchAgents/com.user.pcso.plist with a
StartCalendarInterval of Hour 22, Minute 0, pointing ProgramArguments at
python3 and the script path, then:  launchctl load ~/Library/LaunchAgents/com.user.pcso.plist

### Windows (Task Scheduler)
1. Open "Task Scheduler" -> "Create Basic Task".
2. Name it "PCSO Lotto Update". Trigger: Daily, start time 10:00 PM.
3. Action: "Start a program".
   - Program/script:  python
   - Add arguments:   "C:\path\to\pcso_update.py"
   - Start in:        C:\path\to\
4. Finish. (Tick "Run whether user is logged on or not" if you want it
   to run when you're away.)

## Notes
- The dashboard's "Refresh latest" button tries a live fetch but is usually
  blocked by browser security (CORS) for local files. The reliable update
  path is this Python script on a schedule.
- PCSO publishes results shortly after the 9:00 PM draw, so 10:00 PM is a
  good run time.
- All draw days were derived from PCSO's own published results and match
  the official schedule.

## Disclaimer
Data is sourced from the public PCSO website (pcso.gov.ph) for personal
reference. Always verify against official PCSO results before acting on them.
