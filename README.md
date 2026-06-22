# Data Explorer

Interactive web app for browsing person × project work history from a SQLite database. Filter staff by client, agency, work type, or engagement period; view per-person engagement gantts; download resume-style markdown summaries.

## Quick start (users)

1. Open the app URL (your admin will share it).
2. Click **Browse files** or drag in your `.db` file from a shared folder.
3. Use the sidebar filters to scope the view, then drill into a person via the dropdown.
4. Download resume markdown from the **Markdown export** or **Federal markdown** tabs.

The uploaded file is held in memory for your session only — nothing is stored on the server. No account, no signup.

## Deployment (admin)

### One-time setup

1. Create a new **public** repository on GitHub with these three files at the root:
   - `engagement_gantt_app.py`
   - `requirements.txt`
   - `README.md`
2. Sign in at [share.streamlit.io](https://share.streamlit.io) with the same GitHub account.
3. Click **New app**, pick the repository, set the main file to `engagement_gantt_app.py`, and deploy.
4. After ~2-3 minutes you'll receive a URL. Share it with your team.

A public repository is required for Streamlit Community Cloud's free tier. The repository contains application code only — no data and no database file. The database is supplied by users via the in-app file uploader at runtime.

### Updating the deployed app

Push a commit to the repository. Streamlit Cloud auto-redeploys within ~30 seconds.

### Refreshing the data file

Place the new `.db` file in the shared folder your team uses. Users pick it up the next time they upload to the app. No GitHub commit needed for data updates.

## Expected database schema

The app reads a SQLite database with these tables:

- `people` — one row per person tracked
- `engagements` — engagement windows per (person, project)
- `billable_hours` — daily hours per (person, project, date, activity_code)
- `work_type_transactions` — work-type-classified subset of hours
- `projects` — project roster with metadata
- `project_classifications`, `project_p6_snapshot`, `work_types`, `person_project_roles` — supporting tables

If you upload a file that doesn't match this schema, the app surfaces SQLite errors. The schema is defined by the upstream pipeline that produces the database.

## Tech stack

- [Streamlit](https://streamlit.io) — web app framework
- [Plotly](https://plotly.com/python) — interactive charts (gantts, time series)
- [pandas](https://pandas.pydata.org) — data manipulation
- SQLite (stdlib) — read-only database access
