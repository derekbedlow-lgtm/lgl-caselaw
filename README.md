# lgl-caselaw

Data feed for the Local Government Lawyer Case Law Alert Tracker.

`decisions.json` holds the last 90 days of relevant decisions from [Find Case Law](https://caselaw.nationalarchives.gov.uk) (The National Archives), tagged by topic. It is rebuilt automatically each weekday morning by the workflow in `.github/workflows/refresh.yml`, which runs `build.py`.

The website embed reads the file from:

    https://cdn.jsdelivr.net/gh/derekbedlow-lgtm/lgl-caselaw@main/decisions.json

## Files

- `decisions.json`: the feed. Generated; do not edit by hand.
- `build.py`: fetches the Find Case Law Atom feeds, tags decisions and rewrites `decisions.json`.
- `dismissed.json`: a list of decision ids to leave out of the feed, e.g. `["ewhc-admin-2026-1234"]`. Changing it triggers a rebuild.

Judgment data is Crown copyright and re-used under the Open Justice Licence.
