# KC AIOS Data Explorer

Interactive web app for browsing Kirschner Contractors person × project history. Filter staff by agency, client, work type, or engagement period; view per-person engagement gantts; download resume-style markdown summaries.

## For users (Nancy, Erin, etc.)

1. **Go to the deployed app URL** (Stephen will share).
2. **Sync the latest `kc.db`** from the KC shared OneDrive folder to your local OneDrive cache.
3. **Click "Browse files"** or drag the `kc.db` file from your OneDrive folder into the upload zone.
4. **Use the app:**
   - Sidebar filters: pick person(s), date range, agency (NAVFAC / USACE / Private), client, work type, project, minimum hours/weeks per person on project.
   - "Person focus" mode (default): pick a person, see their engagement history. Tabs include Overview gantt, sortable Records table, Time series, Markdown export (general resume), Federal markdown (NAVFAC/USACE-only resume).
   - "Cross-person QC" mode: project-level views with everyone at once.
5. **Download resume markdown** from the Markdown export or Federal markdown tabs. Paste into Word / proposal templates and clean up as needed.

**Important:** nothing you upload is stored on the server. The file is held in memory for your session and discarded when you close the tab. There's no account, no sign-up, no data persistence.

## For Stephen — deployment

### One-time setup

1. **Create a free GitHub account** (or use an existing one) at [github.com](https://github.com).
2. **Create a new PUBLIC repository.** Name it something like `kc-aios-data-explorer`. Public is required for Streamlit Community Cloud's free tier — there's no client data in the code itself, so this is safe.
3. **Upload these three files to the repo root** (drag into the GitHub web UI):
   - `engagement_gantt_app.py` (copy from `analysis/person-experience/engagement_gantt_app.py`)
   - `requirements.txt` (this directory)
   - `README.md` (this directory)
4. **Deploy via [share.streamlit.io](https://share.streamlit.io):**
   - Sign in with GitHub
   - Click "New app"
   - Pick your repo
   - Main file: `engagement_gantt_app.py`
   - Click Deploy
5. **Wait 2–3 minutes** for first deploy. You'll get a URL like `https://kc-aios-data-explorer-abc123.streamlit.app`.
6. **Test:** open the URL, upload your local `kc.db`, click around. Confirm gantt, tables, markdown export all work.
7. **Share** the URL with Nancy and Erin.

### Monthly refresh

When you have new BQE data:

1. Export BQE on your laptop as usual.
2. Run the kc-aios pipeline (the chain that produces `analysis/kc.db`).
3. Copy `analysis/kc.db` to `~/kirschnercontractors.com/Justin Kirschner - KC B/30 - Metrics/kc.db`. OneDrive syncs it to Nancy's and Erin's machines automatically.
4. Done — Nancy and Erin pick up the new file next time they upload to the app (or just refresh if they have it open).

No GitHub commit needed. The code stays as is; only the data file in OneDrive changes.

### How the app handles the file

When `engagement_gantt_app.py` runs, it picks the kc.db source in this order:

1. **File upload via the in-app uploader** — used by the Streamlit Cloud deploy. Written to a content-hashed temp file; Streamlit's cache picks up same-bytes re-uploads instantly.
2. **OneDrive auto-load** — `~/kirschnercontractors.com/Justin Kirschner - KC B/30 - Metrics/kc.db`. If this file is synced to the user's local OneDrive, the app loads it without a prompt. This path is built with `Path.home()` so it works for any KC user whose laptop has the shared OneDrive synced.
3. **kc-aios local fallback** — `<repo>/analysis/kc.db`. Stephen's pipeline-development workflow: rebuild kc.db locally, app picks it up.

On Streamlit Community Cloud servers, neither local path exists, so the file uploader is always required. The code never reaches outside the user's session — nothing is logged, persisted, or transmitted elsewhere.

When auto-loading, the app shows a small caption naming the source and the file's modification time, so you can spot stale loads at a glance.

## File format

The app expects a SQLite database named `kc.db` with the following tables (built by the kc-aios pipeline):

- `people` — one row per KC staff member
- `engagements` — engagement windows per (person, project)
- `billable_hours` — daily hours per (person, project, date, activity_code)
- `work_type_transactions` — work-type-classified subset of hours
- `projects` — project roster with metadata
- `project_classifications`, `project_p6_snapshot`, `work_types`, `person_project_roles` — supporting tables

If you upload a file that doesn't have these tables, the app will surface SQLite errors. The source of truth for the schema is `analysis/kc-db/build_kc_db.py` in the kc-aios codebase.

## Tech stack

- [Streamlit](https://streamlit.io) — the web app framework
- [Plotly](https://plotly.com/python) — interactive charts (gantts, time series)
- [pandas](https://pandas.pydata.org) — data manipulation
- SQLite (stdlib) — read-only database access
