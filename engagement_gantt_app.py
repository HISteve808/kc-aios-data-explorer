"""KC AIOS data explorer — primary inspection surface for kc.db.

Streamlit app to surface what's actually in kc.db for verification and review.
Two modes:

  Person focus (default): pick a person, see their work history with engagement
  windows AND the underlying daily/weekly hours that compose them. Cross-check
  the periods against the data they're built from. Drill into specific
  (person, project) and (person, work_type) pairs. Export a markdown snapshot
  of the current view for resume / portfolio handoff.

  Cross-person QC: the original engagement-window QC view across all people.
  Useful for spotting anomalies that span people.

Built on kc.db (read-only). Person attribution flows through
work_type_transactions.person (added 2026-05-06 by the canonical-hours merge);
engagements gives window structure (rebuilt 2026-05-06 from canonical parquet).

Run:
    streamlit run analysis/person-experience/engagement_gantt_app.py
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Default kc.db locations checked when no file is uploaded. Path.home() makes
# the OneDrive path work for any KC user (Stephen, Nancy, Erin) since OneDrive
# for Business mounts under the user profile. On Streamlit Cloud neither path
# exists, so the file_uploader is the only source.
DEFAULT_DB_CANDIDATES: list[tuple[str, Path]] = [
    (
        "OneDrive · 30-Metrics",
        Path.home() / "kirschnercontractors.com" / "Justin Kirschner - KC B"
            / "30 - Metrics" / "kc.db",
    ),
    (
        "kc-aios local",
        Path(__file__).resolve().parents[2] / "analysis" / "kc.db",
    ),
]

PROJ_NAME_TRUNC = 40
EMPTY = "(none)"


def _save_uploaded_to_tmp(uploaded_file) -> str:
    """Write uploaded SQLite bytes to a content-hashed tmp path so revisits
    with the same file hit @st.cache_data; new bytes invalidate the cache."""
    content = uploaded_file.getvalue()
    file_hash = hashlib.md5(content).hexdigest()[:16]
    tmp_path = Path(tempfile.gettempdir()) / f"kc_db_{file_hash}.db"
    if not tmp_path.exists():
        tmp_path.write_bytes(content)
    return str(tmp_path)


def _path_meta(p: Path) -> str:
    """Human-readable size + mtime for the source caption."""
    stat = p.stat()
    size_mb = stat.st_size / 1024 / 1024
    mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
    return f"{size_mb:.1f} MB · modified {mtime}"


def _resolve_db_path() -> str | None:
    """Return a path to kc.db, or None if no source is available yet.

    Resolution order:
      1. File upload via st.file_uploader (shared / cloud deployment).
      2. First existing DEFAULT_DB_CANDIDATES entry (local laptop).
    """
    uploaded = st.file_uploader(
        "Upload kc.db",
        type=["db", "sqlite", "sqlite3"],
        help=(
            "Drag in the kc.db file from your OneDrive-synced KC folder. "
            "Held in memory for this session only — nothing is saved on the server."
        ),
    )
    if uploaded is not None:
        size_mb = uploaded.size / 1024 / 1024
        st.success(f"Loaded {uploaded.name} ({size_mb:.1f} MB)")
        return _save_uploaded_to_tmp(uploaded)
    for label, candidate in DEFAULT_DB_CANDIDATES:
        if candidate.exists():
            st.caption(f"Auto-loaded from **{label}** — {_path_meta(candidate)}")
            return str(candidate)
    st.info(
        "Upload your kc.db file to begin. The shared copy lives in OneDrive at "
        "`kirschnercontractors.com / Justin Kirschner - KC B / 30 - Metrics / kc.db`. "
        "Ask Stephen if you don't see it synced."
    )
    return None

# Color palettes — 24+ distinct hues so legends with many categories (16+ work_types,
# 26 staff in cross-person QC with all-people) don't cycle into visual ambiguity.
# Cross-person QC separates active (saturated) from non-active (gray band) so the
# eye lands on current staff first.
WORK_TYPE_COLOR_SEQUENCE = px.colors.qualitative.Dark24
ACTIVE_PERSON_COLORS = px.colors.qualitative.Dark24
NON_ACTIVE_PERSON_GRAYS = ["#9e9e9e", "#bababa", "#7c7c7c", "#a8a8a8", "#888888"]

# Work types excluded from resume-style markdown exports. scheduling_misc is a
# catch-all bucket (codes 270/280/290/295) that doesn't belong on a resume.
# Matched by suffix so every side prefix (contractor_/owner_/sub_) is covered.
RESUME_EXCLUDED_WORK_TYPE_SUFFIX = "scheduling_misc"


# ----------------------------------------------------------------------------
# Data loaders (cached at the connection level)
# ----------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def load_people(db_path: str) -> pd.DataFrame:
    sql = """
        SELECT person, role, tier, still_active,
               first_entry_date, last_entry_date, active_span_years,
               total_hours_alltime, total_hours_24mo,
               n_projects_alltime, n_projects_24mo,
               n_clients_alltime, n_engagement_windows_alltime,
               top_clients_top5_by_hours
        FROM people
        ORDER BY total_hours_alltime DESC NULLS LAST, person
    """
    with sqlite3.connect(db_path) as conn:
        return pd.read_sql(sql, conn)


@st.cache_data(show_spinner=False)
def load_engagements(db_path: str) -> pd.DataFrame:
    sql = """
        SELECT
            e.engagement_id,
            e.person,
            p.tier            AS person_tier,
            e.proj_no,
            e.proj_name,
            e.client_name,
            e.default_side,
            e.project_owner,
            e.window_seq,
            e.start_date,
            e.end_date,
            e.duration_days,
            e.n_active_months,
            e.total_hours,
            e.n_entries
        FROM engagements e
        LEFT JOIN people p ON p.person = e.person
        WHERE e.start_date IS NOT NULL AND e.end_date IS NOT NULL
    """
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql(sql, conn, parse_dates=["start_date", "end_date"])
    return df


@st.cache_data(show_spinner=False)
def load_transactions(db_path: str) -> pd.DataFrame:
    """Person-attributed billable hours (kc.db billable_hours, the FULL canonical
    billable corpus). LEFT JOINs work_type_transactions to attach work_type
    where the row survived classify_work_types.py Step A pre-slice; rows with
    NULL work_type were pre-sliced.

    Total per person matches kc.db.engagements + kc.db.people exactly.

    Carries: proj_no, date, hours, person, activity_code, work_type (nullable),
    default_side, proj_name, client_name, source_match_quality.
    """
    sql = """
        SELECT
            bh.proj_no,
            bh.date,
            bh.hours,
            bh.person,
            wtt.work_type,           -- NULL for rows that didn't survive classification
            bh.activity_code,
            p.default_side,
            p.proj_name              AS proj_name,
            p.client_name,
            p.project_owner,
            bh.source_match_quality,
            pc.classification        AS proj_classification
        FROM billable_hours bh
        LEFT JOIN projects p ON p.proj_no = bh.proj_no
        LEFT JOIN project_classifications pc ON pc.proj_no = bh.proj_no
        LEFT JOIN work_type_transactions wtt
            ON wtt.proj_no = bh.proj_no
           AND wtt.date = bh.date
           AND wtt.activity_code = bh.activity_code
           AND wtt.person = bh.person
        WHERE bh.person IS NOT NULL
    """
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql(sql, conn, parse_dates=["date"])
    # Surface rows that didn't survive classify_work_types.py Step A pre-slice:
    #
    #   classification='exclude' rows (currently just 25-46 Jacob at Turtle Bay
    #   PE, 688 hrs) — real work the classifier deliberately drops from Mission 1
    #   effort modeling because of rate-card billing. Synthesize a side-prefixed
    #   work_type ("owner_field_engineering" for 25-46) so the legend reads as a
    #   normal work_type in the owner color family rather than as an "excluded"
    #   bucket. When the classifier eventually grows a real taxonomy entry for
    #   this work shape (Stephen's Mission 1 phase), drop the synthesis here.
    #
    #   default_side in {OBO, unknown} / NULL rows — genuine data-quality issue
    #   (project's side wasn't resolved). Surfaced as "(Unclassified — side not
    #   mapped)" until fixed upstream in ProjInfo_classified.xlsx.
    df["work_type_or_other"] = df["work_type"]
    mask_unclass = df["work_type_or_other"].isna()
    mask_excluded = mask_unclass & (df["proj_classification"] == "exclude")
    synth_side = df.loc[mask_excluded, "default_side"].fillna("unknown").astype(str)
    df.loc[mask_excluded, "work_type_or_other"] = synth_side + "_field_engineering"
    df.loc[mask_unclass & ~mask_excluded, "work_type_or_other"] = "(Unclassified — side not mapped)"
    return df


@st.cache_data(show_spinner=False)
def load_person_project_roles(db_path: str) -> pd.DataFrame:
    """Phase 5 role inference output. One row per (person, proj_no, period_seq, sub_seq).
    Used by the markdown export tab to filter projects by inferred role."""
    sql = """
        SELECT person, proj_no, proj_name, client_name, default_side,
               period_seq, sub_seq, period_start, period_end,
               hours, pct_of_subperiod, rank_in_subperiod, n_persons_in_sub,
               tier, inferred_role, is_advisory
        FROM person_project_roles
    """
    with sqlite3.connect(db_path) as conn:
        try:
            df = pd.read_sql(sql, conn, parse_dates=["period_start", "period_end"])
        except Exception:
            # Table absent (pipeline not yet run) — return empty
            df = pd.DataFrame(columns=[
                "person", "proj_no", "proj_name", "client_name", "default_side",
                "period_seq", "sub_seq", "period_start", "period_end",
                "hours", "pct_of_subperiod", "rank_in_subperiod", "n_persons_in_sub",
                "tier", "inferred_role", "is_advisory",
            ])
    return df


@st.cache_data(show_spinner=False)
def load_kcdb_snapshot_date(db_path: str) -> str | None:
    sql = "SELECT MAX(snapshot_date) FROM project_p6_snapshot"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(sql).fetchone()
    return row[0] if row else None


@st.cache_data(show_spinner=False)
def load_projects(db_path: str) -> pd.DataFrame:
    """Project-level metadata from the projects table: proj_value plus the
    Stage 4b federal-contract columns. Used by the markdown export tabs as a
    per-proj_no lookup (the engagements table doesn't carry these)."""
    sql = """
        SELECT proj_no, proj_name, project_owner, proj_value,
               contract_number, task_order_number, contract_start, contract_end
        FROM projects
    """
    with sqlite3.connect(db_path) as conn:
        return pd.read_sql(sql, conn, parse_dates=["contract_start", "contract_end"])


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def _trunc(s: str, n: int = PROJ_NAME_TRUNC) -> str:
    if s is None:
        return ""
    s = str(s).replace("\r", " ").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _proj_label(proj_no: str, proj_name: str) -> str:
    return f"{proj_no} — {_trunc(proj_name)}"


def _slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s


def _fmt_window_range(start, end) -> str:
    if pd.isna(start):
        return "—"
    if pd.isna(end):
        return f"{pd.Timestamp(start).date():%Y-%m}"
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    if s.year == e.year and s.month == e.month:
        return f"{s:%Y-%m}"
    return f"{s:%Y-%m} → {e:%Y-%m}"


def _pad_labels_for_left_align(labels: list[str]) -> list[str]:
    """Pad display labels to equal visible length with non-breaking spaces so a
    monospace tick font renders them visually left-aligned (Plotly's y-axis ticks
    are right-aligned by default; uniform-width labels invert that)."""
    import re
    plain_lens = [len(re.sub(r"<[^>]+>", "", lbl)) for lbl in labels]
    max_len = max(plain_lens) if plain_lens else 0
    out = []
    for lbl, plen in zip(labels, plain_lens):
        out.append(lbl + (" " * (max_len - plen)))
    return out


def _merge_window_ranges(starts_ends, gap_days: int = 90):
    """Merge (start, end) date pairs that are within gap_days of each other.
    Returns a list of (start, end) tuples representing contiguous active periods.
    Used by the cross-person header bar to surface 'real' gaps in project activity."""
    pairs = [(pd.Timestamp(s), pd.Timestamp(e)) for s, e in starts_ends if pd.notna(s) and pd.notna(e)]
    if not pairs:
        return []
    pairs.sort()
    merged = [list(pairs[0])]
    for s, e in pairs[1:]:
        cur_start, cur_end = merged[-1]
        if (s - cur_end).days <= gap_days:
            if e > cur_end:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return [tuple(m) for m in merged]


def _format_work_type(wt) -> str:
    """Display a work_type machine label as readable text.
    'contractor_baseline' -> 'Contractor baseline'.
    Preserves all-caps tokens (TIA, CM, KC) by re-uppercasing them after capitalize.
    Bucket labels (those wrapped in parens) pass through unchanged so their
    presentation casing isn't clobbered by .capitalize()."""
    if wt is None or (isinstance(wt, float) and pd.isna(wt)):
        return "—"
    s = str(wt)
    if s.startswith("("):
        return s
    s = s.replace("_", " ").strip().capitalize()
    for tok in (" tia", " cm", " kc", " obo", " p6"):
        s = s.replace(tok, tok.upper())
    return s


def build_person_color_map(people: pd.DataFrame) -> dict[str, str]:
    """Stable color assignment for all persons. Active staff get saturated Dark24
    colors (sorted by total hours desc so most-prominent actives get the most
    distinct hues); non-active staff get muted grays cycling through 5 shades —
    visually subordinate but still hover-distinguishable. Used in Cross-person QC
    Overview gantt and Project Drill In daily-by-person chart so a person keeps
    the same color across views regardless of filter state."""
    color_map: dict[str, str] = {}
    if people.empty:
        return color_map
    active = people[people["still_active"] == 1].sort_values(
        "total_hours_alltime", ascending=False, na_position="last"
    )
    non_active = people[people["still_active"] != 1].sort_values(
        "total_hours_alltime", ascending=False, na_position="last"
    )
    for i, p in enumerate(active["person"]):
        color_map[p] = ACTIVE_PERSON_COLORS[i % len(ACTIVE_PERSON_COLORS)]
    for i, p in enumerate(non_active["person"]):
        color_map[p] = NON_ACTIVE_PERSON_GRAYS[i % len(NON_ACTIVE_PERSON_GRAYS)]
    return color_map


@st.cache_data(show_spinner=False)
def build_work_type_color_map(db_path: str) -> dict[str, str]:
    """Stable color assignment for work_types, grouped by side family so the eye
    reads contractor / owner / sub at a glance and any given work_type keeps the
    same color across all views regardless of filter state.

    Families:
      contractor  → warm (orange → red)
      owner       → cool (purple, blue, green)
      sub         → browns
      other / unknown_*  → grays
      "(Unclassified …)" bucket → mid-gray

    Keys are FORMATTED labels (via _format_work_type), matching what px.timeline
    receives via the color= column."""
    with sqlite3.connect(db_path) as conn:
        wts = [r[0] for r in conn.execute(
            "SELECT work_type FROM work_types ORDER BY work_type"
        ).fetchall()]

    # Synthetic work_types created client-side by load_transactions for rows the
    # classifier pre-sliced (classification='exclude'). Include them in the
    # side-family grouping so they get a color from their side's palette and
    # render alongside real classifier output. Drop these when the classifier
    # grows real taxonomy entries for this work.
    for sw in ("owner_field_engineering", "contractor_field_engineering", "sub_field_engineering"):
        if sw not in wts:
            wts.append(sw)

    contractor_hues = [
        "#fdae6b", "#fd8d3c", "#f16913", "#d94801", "#a63603",
        "#fcae91", "#fb6a4a", "#ef3b2c", "#cb181d", "#a50f15", "#67000d",
    ]
    owner_hues = [
        "#9e9ac8", "#807dba", "#6a51a3", "#54278f",
        "#9ecae1", "#4292c6", "#2171b5", "#08519c",
        "#74c476", "#41ab5d", "#238b45", "#006d2c",
    ]
    sub_hues   = ["#a87850", "#8c6028", "#6e4d1e", "#5a3a14", "#3f250a"]
    other_hues = ["#a0a0a0", "#787878", "#9a9a9a", "#5e5e5e"]

    contractor = sorted([w for w in wts if w.startswith("contractor_")])
    owner      = sorted([w for w in wts if w.startswith("owner_")])
    sub        = sorted([w for w in wts if w.startswith("sub_")])
    other      = sorted([w for w in wts if not w.startswith(("contractor_", "owner_", "sub_"))])

    color_map: dict[str, str] = {}
    for i, w in enumerate(contractor):
        color_map[_format_work_type(w)] = contractor_hues[i % len(contractor_hues)]
    for i, w in enumerate(owner):
        color_map[_format_work_type(w)] = owner_hues[i % len(owner_hues)]
    for i, w in enumerate(sub):
        color_map[_format_work_type(w)] = sub_hues[i % len(sub_hues)]
    for i, w in enumerate(other):
        color_map[_format_work_type(w)] = other_hues[i % len(other_hues)]

    # Data-quality bucket: rows where the project's default_side isn't resolved
    # (OBO / unknown / NULL). Not real classification — kept gray to signal
    # "needs upstream fix" rather than "this is a work category."
    color_map["(Unclassified — side not mapped)"] = "#7e7e7e"
    return color_map


def _build_person_window_breakdown(pt: pd.DataFrame, pe: pd.DataFrame) -> list[dict]:
    """For one person, build per-(project, engagement_window, work_type) rows.

    Each row carries:
      - project + window identity
      - work_type (with display label)
      - outline_start / outline_end (MIN/MAX of this work_type's dates within the window)
      - hours, n_entries
      - weekly: pd.Series(week_period -> hours) for inner solid bars

    The engagement-window structure flows from `engagements` (already 180-day-gap
    detected by build_person_engagements.py), so work_type rows naturally inherit
    the gap-splitting — a work_type that re-engaged after a gap produces multiple
    rows on the same y-stripe in the gantt.
    """
    rows = []
    for _, win in pe.iterrows():
        win_txns = pt[
            (pt["proj_no"] == win["proj_no"]) &
            (pt["date"] >= win["start_date"]) &
            (pt["date"] <= win["end_date"])
        ]
        if win_txns.empty:
            continue
        for wt, wt_grp in win_txns.groupby("work_type_or_other", dropna=False):
            wt_grp = wt_grp.copy()
            wt_grp["_week"] = wt_grp["date"].dt.to_period("W")
            weekly = wt_grp.groupby("_week")["hours"].sum()
            rows.append({
                "proj_no":       win["proj_no"],
                "proj_name":     win["proj_name"],
                "client_name":   win["client_name"],
                "default_side":  win["default_side"],
                "window_seq":    int(win["window_seq"]),
                "win_start":     pd.Timestamp(win["start_date"]),
                "win_end":       pd.Timestamp(win["end_date"]),
                "work_type":     wt,
                "outline_start": pd.Timestamp(wt_grp["date"].min()),
                "outline_end":   pd.Timestamp(wt_grp["date"].max()),
                "hours":         float(wt_grp["hours"].sum()),
                "n_entries":     int(len(wt_grp)),
                "weekly":        weekly,
            })
    return rows


def _person_work_type_rollup(pt: pd.DataFrame) -> pd.DataFrame:
    """One row per (proj_no, work_type_or_other) for a single person's transactions.
    Outer date span (MIN/MAX), hours, n_entries, n_active_months, density, gap_warning.

    Density = active_months / (span_days / 30). gap_warning = density < 0.4 — flags
    rows where the MIN/MAX span likely hides re-engagements that should be split into
    separate windows. v1: naive single-window per group; v2 may add gap-splitting.
    """
    if pt.empty:
        return pt.iloc[0:0].copy()

    grp = (
        pt.assign(_month=pt["date"].dt.to_period("M"))
        .groupby(["proj_no", "work_type_or_other"], dropna=False)
        .agg(
            start=("date", "min"),
            end=("date", "max"),
            hours=("hours", "sum"),
            n_entries=("hours", "size"),
            n_active_months=("_month", "nunique"),
            proj_name=("proj_name", "first"),
            client_name=("client_name", "first"),
            default_side=("default_side", "first"),
        )
        .reset_index()
        .sort_values(["proj_no", "work_type_or_other"])
    )

    # Plotly needs end > start for a visible bar; bump zero-span groups by 1 day.
    same_day = grp["start"] == grp["end"]
    grp.loc[same_day, "end"] = grp.loc[same_day, "start"] + pd.Timedelta(days=1)

    span_days = (grp["end"] - grp["start"]).dt.days.clip(lower=1)
    grp["density"] = (grp["n_active_months"] / (span_days / 30.0)).round(2)
    grp["gap_warning"] = grp["density"] < 0.4
    grp["row_label"] = grp["proj_no"] + " | " + grp["work_type_or_other"].astype(str)
    return grp


# ----------------------------------------------------------------------------
# Person-focus views
# ----------------------------------------------------------------------------
def render_person_overview(
    person: str, eng: pd.DataFrame, txn: pd.DataFrame, people: pd.DataFrame,
    work_type_colors: dict[str, str],
):
    """Header + aggregate cards + engagement gantt for this person."""
    p = people[people["person"] == person]
    if p.empty:
        st.warning(f"Person '{person}' not in kc.db.people.")
        return
    r = p.iloc[0]

    st.markdown(f"### {r['person']}")
    if r["role"]:
        st.markdown(f"**Role:** {r['role']}")
    tenure = (
        f"{pd.Timestamp(r['first_entry_date']):%Y-%m-%d} → {pd.Timestamp(r['last_entry_date']):%Y-%m-%d}"
        if pd.notna(r["first_entry_date"])
        else "—"
    )
    span = f" ({float(r['active_span_years']):.1f} yrs)" if pd.notna(r["active_span_years"]) else ""
    status = " · currently active" if r["still_active"] else ""
    st.markdown(f"**KC tenure:** {tenure}{span}{status}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total hours (all-time)", f"{r['total_hours_alltime']:,.1f}" if pd.notna(r["total_hours_alltime"]) else "—")
    c2.metric("Distinct projects", f"{int(r['n_projects_alltime']):,}" if pd.notna(r["n_projects_alltime"]) else "—")
    c3.metric("Distinct clients", f"{int(r['n_clients_alltime']):,}" if pd.notna(r["n_clients_alltime"]) else "—")
    c4.metric("Engagement windows", f"{int(r['n_engagement_windows_alltime']):,}" if pd.notna(r["n_engagement_windows_alltime"]) else "—")

    if r["still_active"] and pd.notna(r["total_hours_24mo"]) and r["total_hours_24mo"]:
        c1, c2 = st.columns(2)
        c1.metric("Last 24 mo: hours", f"{r['total_hours_24mo']:,.1f}")
        c2.metric("Last 24 mo: projects", f"{int(r['n_projects_24mo']):,}" if pd.notna(r["n_projects_24mo"]) else "—")

    if r.get("top_clients_top5_by_hours"):
        st.markdown(f"**Top clients:** {r['top_clients_top5_by_hours']}")

    st.markdown("---")
    st.markdown("#### Project shape (gantt)")
    pe = eng[eng["person"] == person].copy()
    pt = txn[txn["person"] == person].copy()
    if pe.empty or pt.empty:
        st.info("No engagement / transaction data.")
        return

    breakdown = _build_person_window_breakdown(pt, pe)
    if not breakdown:
        st.info("No (engagement window × work_type) data.")
        return

    proj_sorted = sorted(pe["proj_no"].unique(), reverse=True)

    # Build y-axis in NATURAL reading order (top-to-bottom): newest project header
    # first, then its work_type children ranked by hours desc, then next project, etc.
    # We'll reverse the list at the end so Plotly's default category-axis
    # rendering (bottom-to-top) maps to our desired top-to-bottom display.
    natural_order = []          # internal keys in display top-to-bottom order
    natural_display = []        # parallel display labels
    proj_header_key: dict[str, str] = {}

    for proj_no in proj_sorted:
        proj_pe = pe[pe["proj_no"] == proj_no]
        proj_name = proj_pe["proj_name"].iloc[0]
        header_key = f"__H__{proj_no}"
        proj_header_key[proj_no] = header_key
        natural_order.append(header_key)
        natural_display.append(f"<b>{proj_no} — {_trunc(proj_name, 32)}</b>")

        proj_breakdown = [b for b in breakdown if b["proj_no"] == proj_no]
        if not proj_breakdown:
            continue

        # Rank work_types within project by total hours desc (across all windows).
        wt_total: dict[str, float] = {}
        for b in proj_breakdown:
            wt_total[b["work_type"]] = wt_total.get(b["work_type"], 0.0) + b["hours"]
        wt_ranked = [wt for wt, _ in sorted(wt_total.items(), key=lambda x: -x[1])]

        for wt in wt_ranked:
            wt_label = _format_work_type(wt)
            child_key = f"__C__{proj_no}__{wt}"
            natural_order.append(child_key)
            natural_display.append(f"  └ {wt_label}")
            for b in proj_breakdown:
                if b["work_type"] == wt:
                    b["row_key"] = child_key
                    b["wt_label"] = wt_label

    # Reverse so Plotly's bottom-to-top axis lays out as our intended top-to-bottom.
    y_order = list(reversed(natural_order))
    y_display = list(reversed(natural_display))

    # Weekly bars (the px.timeline layer — provides the legend color mapping).
    weekly_rows = []
    for b in breakdown:
        for week, hrs in b["weekly"].items():
            weekly_rows.append({
                "row_key": b["row_key"],
                "x_start": week.start_time,
                "x_end": week.end_time,
                "hours": float(hrs),
                "wt_label": b["wt_label"],
                "proj_no": b["proj_no"],
                "proj_name": b["proj_name"],
                "window_seq": b["window_seq"],
            })
    weekly_df = pd.DataFrame(weekly_rows)
    if weekly_df.empty:
        st.info("No weekly hours to plot.")
        return

    fig = px.timeline(
        weekly_df,
        x_start="x_start", x_end="x_end",
        y="row_key", color="wt_label",
        color_discrete_map=work_type_colors,
        category_orders={"row_key": y_order},
        hover_data={
            "proj_no": True, "proj_name": True, "window_seq": True,
            "hours": ":.1f", "wt_label": True,
            "x_start": "|%Y-%m-%d", "x_end": False, "row_key": False,
        },
        labels={"wt_label": "Work type"},
    )

    # ── Outline rectangles via fig.layout.shapes (bulk-set, fast) ──────────────
    # Use category-name y0/y1 — Plotly draws a thin band at that category position
    # on a categorical axis. This stays fully in "category mode" instead of mixing
    # in numeric y values that confuse axis range detection.
    shapes = list(fig.layout.shapes or ())
    for b in breakdown:
        shapes.append(dict(
            type="rect",
            x0=pd.Timestamp(b["outline_start"]),
            x1=pd.Timestamp(b["outline_end"]) + pd.Timedelta(days=1),
            y0=b["row_key"], y1=b["row_key"],
            xref="x", yref="y",
            line=dict(color="rgba(80,80,80,0.7)", width=1.75, dash="dot"),
            fillcolor="rgba(0,0,0,0)",
            layer="below",
        ))

    # ── Project header span bars: ONE Scatter trace, all string y values ──────
    hx: list = []
    hy: list = []
    hsym: list = []
    htxt: list = []
    for proj_no in proj_sorted:
        header_key = proj_header_key[proj_no]
        proj_windows = pe[pe["proj_no"] == proj_no].sort_values("start_date")
        for _, win in proj_windows.iterrows():
            s = pd.Timestamp(win["start_date"])
            e = pd.Timestamp(win["end_date"])
            tooltip = (
                f"{proj_no} W{int(win['window_seq'])}<br>"
                f"{s.strftime('%Y-%m-%d')} → {e.strftime('%Y-%m-%d')}<br>"
                f"{win['total_hours']:.1f} hrs"
            )
            hx.extend([s, e, None])
            hy.extend([header_key, header_key, header_key])  # string y, all = header_key
            hsym.extend(["triangle-left", "triangle-right", "circle"])
            htxt.extend([tooltip, tooltip, ""])
    if hx:
        fig.add_trace(go.Scatter(
            x=hx, y=hy,
            mode="lines+markers",
            line=dict(color="rgba(80,80,80,0.85)", width=3),
            marker=dict(color="rgba(80,80,80,0.85)", size=12, symbol=hsym),
            text=htxt,
            hovertemplate="%{text}<extra></extra>",
            showlegend=False,
            name="engagement windows",
        ))

    # ── Axis polish ──────────────────────────────────────────────────────────
    # Pin y axis as category with explicit range so Plotly doesn't auto-adjust;
    # pad ticktext with trailing spaces + monospace font for visual left-align.
    y_display_padded = _pad_labels_for_left_align(y_display)
    fig.update_yaxes(
        title=None,
        type="category",
        range=[-0.5, len(y_order) - 0.5],
        tickfont=dict(size=13, family="monospace"),
        tickmode="array",
        tickvals=y_order,
        ticktext=y_display_padded,
        categoryorder="array",
        categoryarray=y_order,
    )
    fig.update_xaxes(
        title=None,
        side="top",
        showgrid=True,
        gridcolor="rgba(150,150,150,0.35)",
        gridwidth=1,
        dtick="M12",
        tickformat="%Y",
        tickfont=dict(size=12),
    )
    fig.update_layout(
        shapes=shapes,
        height=max(280, 22 * len(y_order) + 100),
        margin=dict(l=10, r=10, t=50, b=10),
        legend_title_text="Work type",
    )
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False})

    n_proj = len(proj_sorted)
    n_wt_rows = sum(1 for k in y_order if k.startswith("__C__"))
    st.caption(
        f"_{n_proj} projects · {n_wt_rows} (project, work_type) child rows · "
        f"newest at top, work_types ranked by total hours within each project. "
        f"Header bars show engagement windows (180-day gap from `engagements`); "
        f"child outlines are MIN→MAX dates within each window; solid bars = weekly hours._"
    )


def render_records(person: str, txn: pd.DataFrame):
    """Sortable per-(project, work_type) records — same data shape as the Overview
    gantt, presented as a flat filterable table. Answers 'when did this person do
    this work_type on this project, for how many hours?'."""
    pt = txn[txn["person"] == person].copy()
    if pt.empty:
        st.info("No billable hours recorded.")
        return

    grp = _person_work_type_rollup(pt)
    if grp.empty:
        st.info("No (project, work_type) groups for this person.")
        return

    display = grp.copy()
    display["start"] = display["start"].dt.strftime("%Y-%m-%d")
    display["end"] = display["end"].dt.strftime("%Y-%m-%d")
    display["hours"] = display["hours"].round(1)

    st.markdown("##### Per-(project, work_type) records")
    st.caption(
        "One row per (project, work_type) for this person. "
        "`start`/`end` are the MIN/MAX dates this person logged that work_type on that project. "
        "`density` = `n_active_months` / (span days / 30). "
        "`gap_warning = True` (density < 0.4) means the span likely hides multi-year gaps "
        "that should be split into separate engagements. "
        "Default sort: ProjNo, then work_type. Click a column header to re-sort."
    )
    st.dataframe(
        display[[
            "proj_no", "proj_name", "client_name", "default_side",
            "work_type_or_other", "start", "end", "hours",
            "n_entries", "n_active_months", "density", "gap_warning",
        ]].rename(columns={
            "proj_no": "ProjNo", "proj_name": "Project name", "client_name": "Client",
            "default_side": "Side", "work_type_or_other": "Work type",
            "start": "Start", "end": "End", "hours": "Hours",
            "n_entries": "Entries", "n_active_months": "Active mos",
            "density": "Density", "gap_warning": "Gap?",
        }),
        use_container_width=True,
        hide_index=True,
    )
    n_gap = int(grp["gap_warning"].sum())
    st.caption(
        f"_{len(grp)} rows · {grp['proj_no'].nunique()} projects · "
        f"{grp['hours'].sum():,.1f} hrs total · {n_gap} flagged with gap_warning_"
    )


def render_time_series(person: str, txn: pd.DataFrame, work_type_colors: dict[str, str]):
    """Stacked time-series of person's full billable hours over time, broken
    down by work_type where classified, with pre-sliced rows surfaced as
    '(non-schedule / pre-sliced)'. Source: kc.db.billable_hours (full canonical
    billable corpus per person), LEFT JOIN work_type_transactions for the
    work_type label."""
    pt = txn[txn["person"] == person].copy()
    if pt.empty:
        st.info("No billable hours recorded for this person.")
        return

    total = pt["hours"].sum()
    classified = pt[pt["work_type"].notna()]["hours"].sum()
    unclassified = total - classified
    c1, c2, c3 = st.columns(3)
    c1.metric("Total billable", f"{total:,.1f}")
    c2.metric("Classified (schedule work)", f"{classified:,.1f}")
    c3.metric("Off-taxonomy hours", f"{unclassified:,.1f}")

    bin_choice = st.radio(
        "Time bin",
        options=["Week", "Month", "Quarter"],
        index=1,
        horizontal=True,
        key="ts_bin",
    )

    color_choice = st.radio(
        "Stack by",
        options=["work_type (with pre-sliced bucket)", "activity_code"],
        index=0,
        horizontal=True,
        key="ts_color",
    )
    color_col = "work_type_or_other" if color_choice.startswith("work_type") else "activity_code"

    pt["bin"] = pt["date"].dt.to_period({"Week": "W", "Month": "M", "Quarter": "Q"}[bin_choice]).dt.to_timestamp()
    grouped = pt.groupby(["bin", color_col], dropna=False)["hours"].sum().reset_index()

    # Use the stable work_type color map when coloring by work_type; fall back to
    # the encounter-order sequence for activity_code (no curated palette there).
    if color_col == "work_type_or_other":
        fig = px.bar(
            grouped, x="bin", y="hours", color=color_col,
            color_discrete_map=work_type_colors,
            labels={"bin": bin_choice, "hours": "Hours", color_col: color_choice.split(" (")[0]},
        )
    else:
        fig = px.bar(
            grouped, x="bin", y="hours", color=color_col,
            color_discrete_sequence=WORK_TYPE_COLOR_SEQUENCE,
            labels={"bin": bin_choice, "hours": "Hours", color_col: color_choice.split(" (")[0]},
        )
    fig.update_layout(
        height=420, barmode="stack",
        margin=dict(l=10, r=10, t=10, b=10),
        legend_title_text=color_choice.split(" (")[0],
    )
    st.plotly_chart(fig, use_container_width=True)

    # Hours-by-work-type summary (uses billable_hours; bucket pre-sliced explicitly)
    st.markdown("##### Hours by work_type (all-time, this person)")
    summary = (
        pt.groupby("work_type_or_other", dropna=False)
        .agg(hours=("hours", "sum"), n_projects=("proj_no", "nunique"), n_rows=("hours", "size"))
        .sort_values("hours", ascending=False)
        .reset_index()
        .rename(columns={"work_type_or_other": "work_type"})
    )
    summary["hours"] = summary["hours"].round(1)
    st.dataframe(summary, use_container_width=True, hide_index=True)
    st.caption(
        "Total matches `people.total_hours_alltime` and `engagements.total_hours` for this person. "
        "Rows the classifier pre-sliced are surfaced two ways: rows from "
        "`project_classifications.classification='exclude'` projects (25-46 Turtle Bay PE) become "
        "**`{side}_field_engineering`** (e.g. `Owner field engineering`) so they read as resume-worthy "
        "work in their side's color family. Rows with `default_side` in {OBO, unknown} or NULL stay in "
        "**`(Unclassified — side not mapped)`** — a data-quality issue, not real classification."
    )


def render_project_drill_in_tab(
    eng: pd.DataFrame, txn: pd.DataFrame, roles: pd.DataFrame,
    person_colors: dict[str, str], work_type_colors: dict[str, str],
):
    """Cross-person Project drill-in tab. Pick a project, see:
      1. Project header
      2. (person, work_type) gantt — outline per (person, engagement window)
         + weekly solid bars colored by work_type. Mirrors Person Overview style
         but rows = (person | work_type) so multiple persons compare side-by-side.
      3. Daily hours by person — second bar chart, stacked by person, no
         work_type detail. The "second bar chart" view Stephen asked for.
      4. Roles table — Phase 5 inferred roles for this project.
      5. Per-person × work_type pivot table — flat data behind the gantt.
    """
    if eng.empty:
        st.info("No engagement data.")
        return

    projs = (
        eng.groupby("proj_no")
        .agg(proj_name=("proj_name", "first"))
        .reset_index()
        .sort_values("proj_no", ascending=False)
    )
    proj_options = ["(select…)"] + [
        f"{r.proj_no} — {_trunc(r.proj_name, 60)}" for r in projs.itertuples()
    ]
    sel = st.selectbox(
        "Pick a project to drill into",
        options=proj_options, index=0, key="proj_detail_picker",
    )
    if sel == "(select…)":
        st.caption(
            "_Pick a project above. You'll get the per-(person, work_type) gantt "
            "(outline + weekly bars), a second multi-person bar chart, the Phase 5 "
            "roles table, and a per-person × work_type breakdown._"
        )
        return
    sel_proj_no = sel.split(" — ")[0]

    pe_p = eng[eng["proj_no"] == sel_proj_no].copy()
    pt_p = txn[txn["proj_no"] == sel_proj_no].copy()
    if pe_p.empty:
        st.info("No engagement data for this project.")
        return

    proj_name = pe_p["proj_name"].iloc[0]
    client = pe_p["client_name"].iloc[0] if pd.notna(pe_p["client_name"].iloc[0]) else "—"
    side = pe_p["default_side"].iloc[0] if pd.notna(pe_p["default_side"].iloc[0]) else "—"
    n_persons = pe_p["person"].nunique()
    total_hrs = pe_p["total_hours"].sum()
    span_start = pe_p["start_date"].min()
    span_end = pe_p["end_date"].max()

    # ── 1. Header ─────────────────────────────────────────────────────────────
    st.markdown(f"### {sel_proj_no} — {proj_name}")
    st.markdown(
        f"**Client:** {client} · **Side:** {side} · "
        f"**Persons:** {n_persons} · **Total engagement hours:** {total_hrs:,.1f} · "
        f"**Span:** {pd.Timestamp(span_start).strftime('%Y-%m-%d')} → {pd.Timestamp(span_end).strftime('%Y-%m-%d')}"
    )

    # ── 2. (person, work_type) gantt with outline + weekly solid bars ────────
    st.markdown("#### Activity by person × work_type")
    # Build per-person breakdown across all persons on this project, then merge
    # into one big list with `person` annotated.
    breakdown: list[dict] = []
    for person in pe_p["person"].unique():
        pe_person = pe_p[pe_p["person"] == person]
        pt_person = pt_p[pt_p["person"] == person]
        person_breakdown = _build_person_window_breakdown(pt_person, pe_person)
        for b in person_breakdown:
            b["person"] = person
            breakdown.append(b)

    if not breakdown:
        st.info("No (person, work_type) breakdown rows.")
    else:
        # Person order = total hours desc on this project
        person_order = (
            pe_p.groupby("person")["total_hours"].sum()
            .sort_values(ascending=False).index.tolist()
        )
        # Build natural display order top-to-bottom: person header → their
        # work_types ranked by hours within the person on this project
        natural_order: list[str] = []
        natural_display: list[str] = []
        person_header_key: dict[str, str] = {}
        for person in person_order:
            header_key = f"__H__{person}"
            person_header_key[person] = header_key
            natural_order.append(header_key)
            natural_display.append(f"<b>{person}</b>")

            person_breakdown = [b for b in breakdown if b["person"] == person]
            if not person_breakdown:
                continue
            wt_total: dict[str, float] = {}
            for b in person_breakdown:
                wt_total[b["work_type"]] = wt_total.get(b["work_type"], 0.0) + b["hours"]
            wt_ranked = [wt for wt, _ in sorted(wt_total.items(), key=lambda x: -x[1])]
            for wt in wt_ranked:
                wt_label = _format_work_type(wt)
                child_key = f"__C__{person}__{wt}"
                natural_order.append(child_key)
                natural_display.append(f"  └ {wt_label}")
                for b in person_breakdown:
                    if b["work_type"] == wt:
                        b["row_key"] = child_key
                        b["wt_label"] = wt_label

        y_order = list(reversed(natural_order))
        y_display = list(reversed(natural_display))

        # Weekly bars (px.timeline → provides the work_type color legend)
        weekly_rows = []
        for b in breakdown:
            for week, hrs in b["weekly"].items():
                weekly_rows.append({
                    "row_key": b["row_key"],
                    "x_start": week.start_time,
                    "x_end": week.end_time,
                    "hours": float(hrs),
                    "wt_label": b["wt_label"],
                    "person": b["person"],
                    "window_seq": b["window_seq"],
                })
        weekly_df = pd.DataFrame(weekly_rows)

        if weekly_df.empty:
            st.info("No weekly hours to plot.")
        else:
            fig_w = px.timeline(
                weekly_df,
                x_start="x_start", x_end="x_end",
                y="row_key", color="wt_label",
                color_discrete_map=work_type_colors,
                category_orders={"row_key": y_order},
                hover_data={
                    "person": True, "window_seq": True,
                    "hours": ":.1f", "wt_label": True,
                    "x_start": "|%Y-%m-%d", "x_end": False, "row_key": False,
                },
                labels={"wt_label": "Work type"},
            )
            # Outline rectangles per (person, engagement window × work_type)
            shapes = list(fig_w.layout.shapes or ())
            for b in breakdown:
                shapes.append(dict(
                    type="rect",
                    x0=pd.Timestamp(b["outline_start"]),
                    x1=pd.Timestamp(b["outline_end"]) + pd.Timedelta(days=1),
                    y0=b["row_key"], y1=b["row_key"],
                    xref="x", yref="y",
                    line=dict(color="rgba(80,80,80,0.7)", width=1.75, dash="dot"),
                    fillcolor="rgba(0,0,0,0)",
                    layer="below",
                ))
            # Person header span bars (one trace, V-ends, all persons concatenated)
            hx, hy, hsym, htxt = [], [], [], []
            for person in person_order:
                header_key = person_header_key[person]
                p_windows = pe_p[pe_p["person"] == person].sort_values("start_date")
                for _, win in p_windows.iterrows():
                    s = pd.Timestamp(win["start_date"])
                    e = pd.Timestamp(win["end_date"])
                    tooltip = (
                        f"{person} W{int(win['window_seq'])}<br>"
                        f"{s.strftime('%Y-%m-%d')} → {e.strftime('%Y-%m-%d')}<br>"
                        f"{win['total_hours']:.1f} hrs"
                    )
                    hx.extend([s, e, None])
                    hy.extend([header_key, header_key, header_key])
                    hsym.extend(["triangle-left", "triangle-right", "circle"])
                    htxt.extend([tooltip, tooltip, ""])
            if hx:
                fig_w.add_trace(go.Scatter(
                    x=hx, y=hy,
                    mode="lines+markers",
                    line=dict(color="rgba(80,80,80,0.85)", width=3),
                    marker=dict(color="rgba(80,80,80,0.85)", size=12, symbol=hsym),
                    text=htxt,
                    hovertemplate="%{text}<extra></extra>",
                    showlegend=False,
                    name="engagement windows",
                ))
            y_display_padded = _pad_labels_for_left_align(y_display)
            fig_w.update_yaxes(
                title=None, type="category",
                range=[-0.5, len(y_order) - 0.5],
                tickfont=dict(size=13, family="monospace"),
                tickmode="array", tickvals=y_order, ticktext=y_display_padded,
                categoryorder="array", categoryarray=y_order,
            )
            fig_w.update_xaxes(
                title=None, side="top",
                showgrid=True, gridcolor="rgba(150,150,150,0.35)",
                gridwidth=1, dtick="M12", tickformat="%Y",
                tickfont=dict(size=12),
            )
            fig_w.update_layout(
                shapes=shapes,
                height=max(280, 22 * len(y_order) + 100),
                margin=dict(l=10, r=10, t=50, b=10),
                legend_title_text="Work type",
            )
            st.plotly_chart(fig_w, use_container_width=True, config={"displaylogo": False})

    # ── 3. Daily hours by person (second bar chart) ──────────────────────────
    st.markdown("#### Daily hours by person")
    if pt_p.empty or pt_p["person"].isna().all():
        st.info("No per-person daily hours for this project.")
    else:
        person_order_d = (
            pe_p.groupby("person")["total_hours"].sum()
            .sort_values(ascending=False).index.tolist()
        )
        daily = (
            pt_p.dropna(subset=["person"])
            .groupby(["date", "person"])["hours"].sum().reset_index()
        )
        fig_d = px.bar(
            daily, x="date", y="hours", color="person",
            color_discrete_map=person_colors,
            category_orders={"person": person_order_d},
            labels={"date": "Date", "hours": "Hours", "person": "Person"},
        )
        fig_d.update_xaxes(
            showgrid=True, gridcolor="rgba(150,150,150,0.35)",
            dtick="M12", tickformat="%Y",
        )
        fig_d.update_layout(
            height=380, barmode="stack",
            margin=dict(l=10, r=10, t=20, b=10),
            legend_title_text="Person",
        )
        st.plotly_chart(fig_d, use_container_width=True)

    # ── 4. Roles table (Phase 5) ─────────────────────────────────────────────
    st.markdown("#### Inferred roles for this project (Phase 5)")
    proj_roles = roles[roles["proj_no"] == sel_proj_no].copy() if not roles.empty else pd.DataFrame()
    if proj_roles.empty:
        st.info("_No Phase 5 role data for this project (sub-threshold or no qualifying contributors)._")
    else:
        rdisp = proj_roles[[
            "person", "tier", "inferred_role", "hours", "pct_of_subperiod",
            "rank_in_subperiod", "n_persons_in_sub", "period_start", "period_end",
        ]].copy()
        rdisp["hours"] = rdisp["hours"].round(1)
        rdisp["pct_of_subperiod"] = (rdisp["pct_of_subperiod"] * 100).round(1)
        rdisp["period_start"] = pd.to_datetime(rdisp["period_start"]).dt.strftime("%Y-%m-%d")
        rdisp["period_end"] = pd.to_datetime(rdisp["period_end"]).dt.strftime("%Y-%m-%d")
        rdisp = rdisp.rename(columns={
            "person": "Person", "tier": "Tier", "inferred_role": "Role",
            "hours": "Hours", "pct_of_subperiod": "% of context",
            "rank_in_subperiod": "Rank", "n_persons_in_sub": "n_in_context",
            "period_start": "Sub-period start", "period_end": "Sub-period end",
        })
        st.dataframe(
            rdisp.sort_values(["Role", "Hours"], ascending=[True, False]),
            use_container_width=True, hide_index=True,
        )

    # ── 5. Per-person work_type breakdown (flat pivot) ──────────────────────
    st.markdown("#### Per-person work_type breakdown")
    if pt_p.empty or pt_p["person"].isna().all():
        st.info("_No per-person transactions for this project._")
    else:
        wt_p = (
            pt_p.dropna(subset=["person"])
            .groupby(["person", "work_type_or_other"])["hours"].sum()
            .reset_index()
        )
        pivot = wt_p.pivot_table(
            index="person", columns="work_type_or_other", values="hours",
            aggfunc="sum", fill_value=0,
        ).round(1)
        pivot["TOTAL"] = pivot.sum(axis=1).round(1)
        pivot = pivot.sort_values("TOTAL", ascending=False)
        col_totals = pivot.drop(columns=["TOTAL"]).sum().sort_values(ascending=False)
        pivot = pivot[col_totals.index.tolist() + ["TOTAL"]]
        pivot.columns = [_format_work_type(c) if c != "TOTAL" else "TOTAL" for c in pivot.columns]
        st.dataframe(
            pivot.reset_index().rename(columns={"person": "Person"}),
            use_container_width=True, hide_index=True,
        )


def render_markdown_export(
    person: str,
    eng: pd.DataFrame,
    txn: pd.DataFrame,
    people: pd.DataFrame,
    roles: pd.DataFrame,
    projects: pd.DataFrame,
    snapshot_date: str | None,
    federal_only: bool = False,
):
    """Person resume-style markdown export.

    federal_only=True restricts the portfolio to NAVFAC/USACE projects, adds
    contract_number / task_order_number / award date to each project block, and
    swaps the all-time career snapshot for a federal-scoped portfolio snapshot.
    """
    key_suffix = "_federal" if federal_only else ""

    p = people[people["person"] == person]
    if p.empty:
        return
    r = p.iloc[0]
    pe = eng[eng["person"] == person].copy()
    pt = txn[txn["person"] == person].copy()
    # Resume hygiene: scheduling_misc work doesn't belong on a resume — drop it
    # from pt so it's absent from both the per-project work-type breakdown and
    # the capability-surface table (both read pt.work_type_or_other).
    pt = pt[
        ~pt["work_type_or_other"].astype(str).str.endswith(RESUME_EXCLUDED_WORK_TYPE_SUFFIX)
    ].copy()
    p_roles = roles[roles["person"] == person].copy() if not roles.empty else pd.DataFrame()

    # Project-level metadata (proj_value + Stage 4b federal-contract columns), by proj_no.
    proj_meta = projects.set_index("proj_no").to_dict("index") if not projects.empty else {}
    fed_projs = (
        set(projects.loc[projects["project_owner"].isin(["NAVFAC", "USACE"]), "proj_no"])
        if not projects.empty else set()
    )

    # ── Role filter UI (Phase 5 integration) ──────────────────────────────────
    st.markdown("##### Export filters")
    if not p_roles.empty:
        all_roles_for_person = sorted(p_roles["inferred_role"].unique())
        # Default: include everything except Oversight (QC-style work)
        default_roles = [x for x in all_roles_for_person if x != "Oversight"]
        sel_roles = st.multiselect(
            "Roles to include in export",
            options=all_roles_for_person,
            default=default_roles,
            key=f"role_filter_{person}{key_suffix}",
            help=(
                "Lead — primary contributor (rank 1, ≥10% of period hours). "
                "Support — secondary contributor on someone else's lead project. "
                "Oversight — higher-tier review/QC pattern (off by default). "
                "Contributor — collaborative advisory work (e.g., 25-31 Turtle Bay)."
            ),
        )
        included_proj_periods = p_roles[p_roles["inferred_role"].isin(sel_roles)]
        included_projs = set(included_proj_periods["proj_no"].unique())
        st.caption(
            f"_{len(included_proj_periods)} project-period rows match · "
            f"{len(included_projs)} distinct projects · "
            f"{int(included_proj_periods['hours'].sum()):,} hrs in scope_"
        )
    else:
        sel_roles = []
        included_proj_periods = pd.DataFrame()
        included_projs = set(pe["proj_no"].unique())  # no role data → no filter
        st.warning("No role data — pipeline may not have run. Showing all projects.")

    # Federal-only restriction: intersect the in-scope set with NAVFAC/USACE projects.
    if federal_only:
        before_n = len(included_projs)
        included_projs = included_projs & fed_projs
        if not included_proj_periods.empty:
            included_proj_periods = included_proj_periods[
                included_proj_periods["proj_no"].isin(fed_projs)
            ]
        st.caption(
            f"_Federal filter (NAVFAC/USACE): {len(included_projs)} of {before_n} "
            f"in-scope projects_"
        )

    # Per-project rollup (engagement-period centric, ProjNo descending = newest first)
    proj_hrs = pt.groupby("proj_no")["hours"].sum().rename("txn_hours")
    by_proj = (
        pe[pe["proj_no"].isin(included_projs)]
        .groupby("proj_no")
        .agg(
            proj_name=("proj_name", "first"),
            client_name=("client_name", "first"),
            default_side=("default_side", "first"),
            first_eng=("start_date", "min"),
            last_eng=("end_date", "max"),
            eng_hours=("total_hours", "sum"),
            n_windows=("engagement_id", "count"),
        )
        .join(proj_hrs)
        .fillna({"txn_hours": 0})
        .reset_index()
        .sort_values("proj_no", ascending=False)
    )

    # Per-project window list (preserve sequence so W1, W2, W3 print in order).
    windows_per_proj = {
        proj: g.sort_values("window_seq")[["window_seq", "start_date", "end_date", "total_hours"]].to_dict("records")
        for proj, g in pe.groupby("proj_no")
    }

    # Per-project role rows (only the ones in the selected roles)
    role_rows_by_proj: dict[str, list[dict]] = {}
    if not included_proj_periods.empty:
        for proj, g in included_proj_periods.groupby("proj_no"):
            role_rows_by_proj[proj] = g.sort_values(["period_seq", "sub_seq"]).to_dict("records")

    # Per-(proj, engagement_window, work_type) breakdown — same structure as the
    # Overview gantt's child rows, so the export mirrors what's on screen.
    breakdown = _build_person_window_breakdown(pt, pe) if (not pe.empty and not pt.empty) else []
    breakdown_by_proj: dict[str, list[dict]] = {}
    for b in breakdown:
        breakdown_by_proj.setdefault(b["proj_no"], []).append(b)

    out = []
    out.append(f"# {r['person']}")
    out.append("")
    if federal_only:
        out.append("**Federal projects — NAVFAC / USACE**")
        out.append("")
    if r["role"]:
        out.append(f"**Role:** {r['role']}")
    tenure_first = pd.Timestamp(r["first_entry_date"]).strftime("%Y-%m-%d") if pd.notna(r["first_entry_date"]) else "—"
    tenure_last = pd.Timestamp(r["last_entry_date"]).strftime("%Y-%m-%d") if pd.notna(r["last_entry_date"]) else "—"
    span = f" ({float(r['active_span_years']):.1f} yrs)" if pd.notna(r["active_span_years"]) else ""
    status = " · currently active" if r["still_active"] else ""
    out.append(f"**KC tenure:** {tenure_first} → {tenure_last}{span}{status}")
    out.append("")
    if federal_only:
        out.append("## Federal portfolio snapshot (NAVFAC / USACE)")
        out.append("")
        if by_proj.empty:
            out.append("- _No NAVFAC/USACE project work in scope._")
        else:
            out.append(f"- Total hours on federal projects: {by_proj['eng_hours'].sum():,.1f}")
            out.append(f"- Federal projects: {len(by_proj):,}")
            out.append(f"- Distinct federal clients: {by_proj['client_name'].nunique():,}")
            out.append(f"- Engagement windows: {int(by_proj['n_windows'].sum()):,}")
        out.append("")
    else:
        out.append("## Career snapshot")
        out.append("")
        out.append(f"- Total hours (all-time): {r['total_hours_alltime']:,.1f}" if pd.notna(r["total_hours_alltime"]) else "- Total hours (all-time): —")
        out.append(f"- Distinct projects: {int(r['n_projects_alltime']):,}" if pd.notna(r["n_projects_alltime"]) else "- Distinct projects: —")
        out.append(f"- Distinct clients: {int(r['n_clients_alltime']):,}" if pd.notna(r["n_clients_alltime"]) else "- Distinct clients: —")
        out.append(f"- Engagement windows: {int(r['n_engagement_windows_alltime']):,}" if pd.notna(r["n_engagement_windows_alltime"]) else "- Engagement windows: —")
        if r["still_active"] and pd.notna(r["total_hours_24mo"]) and r["total_hours_24mo"]:
            out.append(f"- Last 24 months: {r['total_hours_24mo']:,.1f} hrs across {int(r['n_projects_24mo']):,} projects")
        out.append("")

    out.append("## Project portfolio")
    out.append("")
    scope_note = " · NAVFAC/USACE only" if federal_only else ""
    if sel_roles:
        out.append(f"Filtered to roles: **{', '.join(sel_roles)}**{scope_note}. ProjNo descending (newest first).")
    else:
        out.append(f"Sorted by ProjNo descending (newest first){scope_note}.")
    out.append("")
    if by_proj.empty:
        if federal_only:
            out.append("_No NAVFAC/USACE project work matching the selected roles._")
        else:
            out.append("_No logged project work in BQE matching the selected roles._")
    else:
        for _, row in by_proj.iterrows():
            proj_no = row["proj_no"]
            label = f"{proj_no} — {row['proj_name']}" if pd.notna(row["proj_name"]) else proj_no
            out.append(f"### {label}")
            client = row["client_name"] if pd.notna(row["client_name"]) else "—"
            side = row["default_side"] if pd.notna(row["default_side"]) else "—"
            out.append(f"- Client: {client} · Side: {side}")
            # Project value (projects.proj_value — reconciled federal value for NAVFAC/USACE).
            meta = proj_meta.get(proj_no, {})
            pv = meta.get("proj_value")
            if pd.notna(pv):
                out.append(f"- Project value: ${pv:,.0f}")
            # Federal contract identifiers + award date.
            if federal_only:
                cn = meta.get("contract_number")
                to = meta.get("task_order_number")
                cs = meta.get("contract_start")
                if pd.notna(cn) and str(cn).strip():
                    cline = f"- Contract: {cn}"
                    if pd.notna(to) and str(to).strip():
                        cline += f" · Task order: {to}"
                    out.append(cline)
                if pd.notna(cs):
                    out.append(f"- Award date: {pd.Timestamp(cs).strftime('%Y-%m-%d')}")
            # Role line(s) — one per (period, sub) row that matched the filter.
            role_rows = role_rows_by_proj.get(proj_no, [])
            if role_rows:
                if len(role_rows) == 1:
                    rr = role_rows[0]
                    p_start = pd.Timestamp(rr["period_start"]).strftime("%Y-%m-%d")
                    p_end = pd.Timestamp(rr["period_end"]).strftime("%Y-%m-%d")
                    out.append(
                        f"- Role: **{rr['inferred_role']}** "
                        f"({p_start} → {p_end}, {rr['hours']:,.1f} hrs, "
                        f"{rr['pct_of_subperiod']*100:.0f}% of period)"
                    )
                else:
                    out.append("- Roles by period:")
                    for rr in role_rows:
                        p_start = pd.Timestamp(rr["period_start"]).strftime("%Y-%m-%d")
                        p_end = pd.Timestamp(rr["period_end"]).strftime("%Y-%m-%d")
                        out.append(
                            f"  - **{rr['inferred_role']}** "
                            f"({p_start} → {p_end}, {rr['hours']:,.1f} hrs, "
                            f"{rr['pct_of_subperiod']*100:.0f}% of period)"
                        )
            period = _fmt_window_range(row["first_eng"], row["last_eng"])
            n_win = int(row["n_windows"])
            out.append(f"- Engagement period: {period} ({n_win} window{'s' if n_win != 1 else ''})")
            wins = windows_per_proj.get(proj_no, [])
            if n_win > 1:
                for w in wins:
                    w_start = pd.Timestamp(w["start_date"]).strftime("%Y-%m-%d")
                    w_end = pd.Timestamp(w["end_date"]).strftime("%Y-%m-%d")
                    out.append(
                        f"  - W{int(w['window_seq'])}: {w_start} → {w_end} ({w['total_hours']:,.1f} hrs)"
                    )
            out.append(f"- Total hours: {row['eng_hours']:,.1f}")
            # Work-type breakdown: per (engagement window × work_type), so a re-engaged
            # work_type shows up as a separate line rather than spanning the gap.
            proj_breakdown = breakdown_by_proj.get(proj_no, [])
            if proj_breakdown:
                out.append("- Work-type breakdown:")
                # Sort by (window_seq, hours desc) so windows print in order with
                # within-window work_types ranked.
                for b in sorted(proj_breakdown, key=lambda x: (x["window_seq"], -x["hours"])):
                    wt_label = _format_work_type(b["work_type"])
                    wt_start = b["outline_start"].strftime("%Y-%m-%d")
                    wt_end = b["outline_end"].strftime("%Y-%m-%d")
                    win_tag = f" (W{b['window_seq']})" if n_win > 1 else ""
                    out.append(
                        f"  - {wt_label}{win_tag}: {wt_start} → {wt_end} — {b['hours']:,.1f} hrs"
                    )
            out.append("")

    out.append("## Work-type capability surface")
    out.append("")
    pt_surface = pt[pt["proj_no"].isin(included_projs)] if federal_only else pt
    if pt_surface.empty:
        out.append("_No work-type data._")
    else:
        wt_summary = (
            pt_surface.groupby("work_type_or_other", dropna=False)
            .agg(hours=("hours", "sum"), n_projects=("proj_no", "nunique"))
            .sort_values("hours", ascending=False)
        )
        out.append("| Work type | Hours | Projects |")
        out.append("|---|---:|---:|")
        for wt, srow in wt_summary.iterrows():
            out.append(f"| {_format_work_type(wt)} | {srow['hours']:,.1f} | {int(srow['n_projects'])} |")
        out.append("")

    out.append("---")
    if snapshot_date:
        out.append(f"_Source: kc.db (snapshot {snapshot_date}). Generated from KC AIOS data explorer._")

    md_content = "\n".join(out)
    st.markdown("##### Preview")
    st.code(md_content, language="markdown")
    dl_suffix = "-federal" if federal_only else ""
    st.download_button(
        label=f"Download {_slugify(person)}{dl_suffix}.md",
        data=md_content.encode("utf-8"),
        file_name=f"{_slugify(person)}{dl_suffix}.md",
        mime="text/markdown",
        key=f"download_md_{person}{key_suffix}",
    )


# ----------------------------------------------------------------------------
# Cross-person QC view — hierarchical gantt (project header → person rows)
# ----------------------------------------------------------------------------
def render_cross_person_gantt(eng: pd.DataFrame, person_colors: dict[str, str]):
    """Cross-person QC gantt with project headers (90-day-merged spans, V-ends)
    and person children (one row per (project, person), engagement windows as
    solid bars). Project order: ProjNo DESC. Person order within project:
    by total hours DESC (lead-vs-support signal)."""
    if eng.empty:
        st.info("No engagements to display.")
        return

    proj_sorted = sorted(eng["proj_no"].unique(), reverse=True)

    # Build natural display order (top-to-bottom), then reverse for Plotly's
    # bottom-to-top axis. Same pattern as Person Overview gantt.
    natural_order: list[str] = []
    natural_display: list[str] = []
    proj_header_key: dict[str, str] = {}
    proj_merged_spans: dict[str, list] = {}
    person_proj_total = (
        eng.groupby(["proj_no", "person"])["total_hours"].sum().reset_index()
    )

    for proj_no in proj_sorted:
        proj_eng = eng[eng["proj_no"] == proj_no]
        proj_name = proj_eng["proj_name"].iloc[0]
        header_key = f"__H__{proj_no}"
        proj_header_key[proj_no] = header_key
        natural_order.append(header_key)
        natural_display.append(f"<b>{proj_no} — {_trunc(proj_name, 32)}</b>")

        # Project header span: merge engagement windows across all people within
        # 90 days (cross-person 'real gap' threshold).
        proj_merged_spans[proj_no] = _merge_window_ranges(
            list(zip(proj_eng["start_date"], proj_eng["end_date"])),
            gap_days=90,
        )

        # Person child rows: rank by total hours desc on this project.
        proj_persons = (
            person_proj_total[person_proj_total["proj_no"] == proj_no]
            .sort_values("total_hours", ascending=False)
        )
        for _, p in proj_persons.iterrows():
            child_key = f"__C__{proj_no}__{p['person']}"
            natural_order.append(child_key)
            natural_display.append(f"  └ {p['person']}")

    y_order = list(reversed(natural_order))
    y_display = list(reversed(natural_display))

    eng_disp = eng.copy()
    eng_disp["row_key"] = "__C__" + eng_disp["proj_no"].astype(str) + "__" + eng_disp["person"].astype(str)

    fig = px.timeline(
        eng_disp,
        x_start="start_date", x_end="end_date",
        y="row_key", color="person",
        color_discrete_map=person_colors,
        category_orders={"row_key": y_order},
        hover_data={
            "proj_no": True, "proj_name": True, "person": True,
            "client_name": True, "default_side": True, "window_seq": True,
            "total_hours": ":.1f", "n_active_months": True,
            "start_date": "|%Y-%m-%d", "end_date": "|%Y-%m-%d",
            "row_key": False,
        },
        labels={"row_key": "Person"},
    )

    # ── Project header span bars: ONE trace, string y values ─────────────────
    hx: list = []
    hy: list = []
    hsym: list = []
    htxt: list = []
    for proj_no in proj_sorted:
        header_key = proj_header_key[proj_no]
        for span_start, span_end in proj_merged_spans[proj_no]:
            s = pd.Timestamp(span_start)
            e = pd.Timestamp(span_end)
            tooltip = (
                f"{proj_no}<br>"
                f"{s.strftime('%Y-%m-%d')} → {e.strftime('%Y-%m-%d')}<br>"
                f"(merged span, 90-day gap across all people)"
            )
            hx.extend([s, e, None])
            hy.extend([header_key, header_key, header_key])
            hsym.extend(["triangle-left", "triangle-right", "circle"])
            htxt.extend([tooltip, tooltip, ""])
    if hx:
        fig.add_trace(go.Scatter(
            x=hx, y=hy,
            mode="lines+markers",
            line=dict(color="rgba(80,80,80,0.85)", width=3),
            marker=dict(color="rgba(80,80,80,0.85)", size=12, symbol=hsym),
            text=htxt,
            hovertemplate="%{text}<extra></extra>",
            showlegend=False,
            name="project span",
        ))

    # ── Axis polish ──────────────────────────────────────────────────────────
    y_display_padded = _pad_labels_for_left_align(y_display)
    fig.update_yaxes(
        title=None,
        type="category",
        range=[-0.5, len(y_order) - 0.5],
        tickfont=dict(size=13, family="monospace"),
        tickmode="array",
        tickvals=y_order,
        ticktext=y_display_padded,
        categoryorder="array",
        categoryarray=y_order,
    )
    fig.update_xaxes(
        title=None,
        side="top",
        showgrid=True,
        gridcolor="rgba(150,150,150,0.35)",
        gridwidth=1,
        dtick="M12",
        tickformat="%Y",
        tickfont=dict(size=12),
    )
    fig.update_layout(
        height=max(400, 20 * len(y_order) + 100),
        margin=dict(l=10, r=10, t=50, b=10),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displaylogo": False})

    n_proj = len(proj_sorted)
    n_person_rows = sum(1 for k in y_order if k.startswith("__C__"))
    st.caption(
        f"_{n_proj} projects · {n_person_rows} (project, person) child rows · "
        f"newest at top, persons ranked by hours within each project. "
        f"Header spans merge engagement windows across all people within 90 days; "
        f"child bars show each person's engagement windows (180-day gap from `engagements`)._"
    )


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="KC AIOS — Data Explorer", layout="wide")
    st.title("KC AIOS — Data Explorer")
    st.caption("Inspection surface for kc.db. Person-attributed transactions + engagement windows.")

    # File upload (shared / cloud deployment) OR local fallback (Stephen's laptop).
    db_path = _resolve_db_path()
    if db_path is None:
        st.stop()

    snapshot_date = load_kcdb_snapshot_date(db_path)
    if snapshot_date:
        st.caption(f"kc.db snapshot: {snapshot_date}")

    people = load_people(db_path)
    person_colors = build_person_color_map(people)
    work_type_colors = build_work_type_color_map(db_path)
    eng = load_engagements(db_path)
    txn = load_transactions(db_path)
    roles = load_person_project_roles(db_path)
    projects_df = load_projects(db_path)  # project-level metadata; NOT the sidebar's proj_no list

    # Sidebar: mode + filters
    with st.sidebar:
        st.header("Mode")
        mode = st.radio("View", ["Person focus", "Cross-person QC"], index=0, key="mode")

        st.header("Filters")
        active_only = st.checkbox(
            "Active staff only", value=True,
            help="Hides people with still_active=0 AND people whose tier contains "
                 "'overhead' (Erin, Kim, Nancy, etc.) — overhead workers may still "
                 "log time but aren't doing project work for resume purposes."
        )
        if active_only:
            tier_str = people["tier"].fillna("").astype(str).str.lower()
            active_mask = (people["still_active"] == 1) & (~tier_str.str.contains("overhead"))
            active_people = set(people[active_mask]["person"])
            eng = eng[eng["person"].isin(active_people)]
            txn = txn[txn["person"].isin(active_people)]
            people = people[people["person"].isin(active_people)]

        # Person multiselect — empty = no filter; populated = restrict to those
        # persons across both modes. In Person focus this constrains the dropdown
        # options; in Cross-person QC it filters who appears in the gantt.
        person_options = sorted(people["person"].dropna().unique())
        sel_persons = st.multiselect(
            "Persons (multi-select; leave empty for all)",
            options=person_options, default=[],
        )
        if sel_persons:
            eng = eng[eng["person"].isin(sel_persons)]
            txn = txn[txn["person"].isin(sel_persons)]
            people = people[people["person"].isin(sel_persons)]

        # Date range
        date_min = pd.Timestamp(eng["start_date"].min()).date() if not eng.empty else date(2018, 1, 1)
        date_max = pd.Timestamp(eng["end_date"].max()).date() if not eng.empty else date.today()
        date_range = st.date_input(
            "Date range", value=(date_min, date_max), min_value=date_min, max_value=date_max
        )
        if isinstance(date_range, tuple) and len(date_range) == 2:
            d_lo, d_hi = pd.Timestamp(date_range[0]), pd.Timestamp(date_range[1])
            eng = eng[(eng["end_date"] >= d_lo) & (eng["start_date"] <= d_hi)]
            txn = txn[(txn["date"] >= d_lo) & (txn["date"] <= d_hi)]

        # Side filter
        sides = sorted([s for s in eng["default_side"].dropna().unique() if s])
        if sides:
            sel_sides = st.multiselect("Side", options=sides, default=sides)
            eng = eng[eng["default_side"].isin(sel_sides)]
            txn = txn[txn["default_side"].isin(sel_sides)]

        # Agency / project owner filter (NAVFAC, USACE, HDOT, C&C, HART, Private, etc.).
        # Empty = no filter. Useful for agency-specific resume exports.
        owners = sorted([o for o in eng["project_owner"].dropna().unique() if o])
        sel_owners = st.multiselect(
            "Agency / project owner", options=owners, default=[],
            help="Filter to projects with these owners. Useful for USACE/NAVFAC-only resumes. "
                 "Note 'Private' is a catchall — combine with Client name filter for precision.",
        )
        if sel_owners:
            keep_projs = set(eng[eng["project_owner"].isin(sel_owners)]["proj_no"].unique())
            eng = eng[eng["proj_no"].isin(keep_projs)]
            txn = txn[txn["proj_no"].isin(keep_projs)]

        # Client name filter (Hensel Phelps, Capriati, W.W. Clyde, etc.).
        clients = sorted([c for c in eng["client_name"].dropna().unique() if c])
        sel_clients = st.multiselect(
            "Client name", options=clients, default=[],
            help="Filter to projects with these direct clients (the contracting party).",
        )
        if sel_clients:
            keep_projs = set(eng[eng["client_name"].isin(sel_clients)]["proj_no"].unique())
            eng = eng[eng["proj_no"].isin(keep_projs)]
            txn = txn[txn["proj_no"].isin(keep_projs)]

        # Project filter
        projects = sorted(eng["proj_no"].dropna().unique())
        sel_projects = st.multiselect("Project (proj_no)", options=projects, default=[])
        if sel_projects:
            eng = eng[eng["proj_no"].isin(sel_projects)]
            txn = txn[txn["proj_no"].isin(sel_projects)]

        # Work-type filter — operates on work_type_or_other so synthetic labels
        # (e.g. "owner_field_engineering") and the side-not-mapped bucket are
        # selectable, not just rows the classifier produced.
        work_types = sorted([w for w in txn["work_type_or_other"].dropna().unique() if w])
        sel_wts = st.multiselect("Work type", options=work_types, default=[])
        if sel_wts:
            txn = txn[txn["work_type_or_other"].isin(sel_wts)]

        # Person-level (person, project) filters. Both operate on (person, proj_no)
        # pairs by summing across that person's engagement windows on the project
        # (gaps between windows ignored). A pair must pass BOTH thresholds to keep.
        # In Cross-person QC the project header disappears only when no person
        # under it qualifies.
        min_proj_hrs = st.slider(
            "Min hours per person on project", min_value=0, max_value=200, value=0, step=10,
            help="Hide (person, project) pairs where the person's total engagement hours on the project are below this threshold."
        )
        min_weeks = st.slider(
            "Min engagement weeks per person on project",
            min_value=0, max_value=200, value=0, step=1,
            help="Hide (person, project) pairs where the person's total engagement duration (sum of window days ÷ 7, rounded) is below this threshold."
        )
        if not eng.empty and (min_proj_hrs > 0 or min_weeks > 0):
            pair_agg = eng.groupby(["person", "proj_no"]).agg(
                total_hours=("total_hours", "sum"),
                total_days=("duration_days", "sum"),
            )
            pair_agg["rounded_weeks"] = (pair_agg["total_days"] / 7).round().astype(int)
            keep_pairs = pair_agg[
                (pair_agg["total_hours"] >= min_proj_hrs)
                & (pair_agg["rounded_weeks"] >= min_weeks)
            ].index
            eng = eng[pd.MultiIndex.from_arrays([eng["person"], eng["proj_no"]]).isin(keep_pairs)]
            txn = txn[pd.MultiIndex.from_arrays([txn["person"], txn["proj_no"]]).isin(keep_pairs)]

    # Main panel
    if mode == "Person focus":
        person_options = list(people.sort_values("total_hours_alltime", ascending=False, na_position="last")["person"])
        if not person_options:
            st.warning("No people in current filter.")
            return
        person = st.selectbox("Person", options=person_options, index=0, key="person")

        tabs = st.tabs([
            "Overview", "Records", "Time series", "Markdown export", "Federal markdown",
        ])
        with tabs[0]:
            render_person_overview(person, eng, txn, people, work_type_colors)
        with tabs[1]:
            render_records(person, txn)
        with tabs[2]:
            render_time_series(person, txn, work_type_colors)
        with tabs[3]:
            render_markdown_export(person, eng, txn, people, roles, projects_df, snapshot_date)
        with tabs[4]:
            render_markdown_export(
                person, eng, txn, people, roles, projects_df, snapshot_date,
                federal_only=True,
            )
    else:
        cp_tabs = st.tabs(["Overview", "Project Drill In"])
        with cp_tabs[0]:
            st.subheader("Cross-person engagement gantt")
            render_cross_person_gantt(eng, person_colors)
        with cp_tabs[1]:
            render_project_drill_in_tab(eng, txn, roles, person_colors, work_type_colors)


if __name__ == "__main__":
    main()
