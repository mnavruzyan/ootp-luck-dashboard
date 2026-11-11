"""
OOTP GM Fortune Dashboard — Streamlit app.

Inputs:  StatsPlus reports URL + API token (+ optional display name)
Output:  Downloadable self-contained HTML dashboard
"""

import json
import os
import pathlib
import streamlit as st

from pipeline import run_pipeline, parse_reports_url

# ─────────────────────────────────────────────────────────────
TEMPLATE_PATH = pathlib.Path(__file__).parent / "dashboard_template.html"

st.set_page_config(
    page_title="OOTP GM Fortune Dashboard",
    page_icon="⚾",
    layout="centered",
)

# ─────────────────────────────────────────────────────────────
# Sidebar — inputs
# ─────────────────────────────────────────────────────────────
st.title("⚾ OOTP GM Fortune Dashboard")
st.markdown(
    "Generate a shareable playoff luck analysis for any OOTP league on StatsPlus. "
    "All you need is the league's reports URL and your API token."
)

with st.form("config_form"):
    st.subheader("League configuration")

    reports_url = st.text_input(
        "StatsPlus Reports URL",
        placeholder="https://atl-01.statsplus.net/myleague/reports/news/html/coaches/human_manager_1.html",
        help="Any page from your league's StatsPlus reports site. Used to detect the server and league slug.",
    )

    api_token = st.text_input(
        "StatsPlus API Token",
        type="password",
        placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        help="Found in your StatsPlus league settings under API access.",
    )

    league_display_name = st.text_input(
        "League display name (optional)",
        placeholder="Skyline Baseball League",
        help="Shown in the dashboard header. Defaults to the league slug if blank.",
    )

    min_appearances = st.slider(
        "Minimum playoff appearances to include a GM",
        min_value=1, max_value=10, value=3,
        help="GMs with fewer appearances are excluded from the rankings.",
    )

    submitted = st.form_submit_button("Generate Dashboard", type="primary")

# ─────────────────────────────────────────────────────────────
# Pipeline
# ─────────────────────────────────────────────────────────────
if submitted:
    if not reports_url or not api_token:
        st.error("Reports URL and API token are both required.")
        st.stop()

    try:
        host, slug = parse_reports_url(reports_url)
    except Exception:
        st.error("Could not parse the reports URL. Make sure it's a full https:// URL from your league's StatsPlus reports site.")
        st.stop()

    display_name = league_display_name.strip() or slug

    status_box = st.empty()
    bar = st.progress(0.0)

    progress = {"status": status_box, "bar": bar}

    with st.spinner("Running pipeline…"):
        try:
            gm_stats = run_pipeline(
                reports_url=reports_url,
                api_token=api_token,
                progress=progress,
                min_appearances=min_appearances,
            )
        except Exception as e:
            status_box.error(f"Pipeline failed: {e}")
            st.exception(e)
            st.stop()

    bar.progress(1.0)
    status_box.success(f"Done — {len(gm_stats)} GMs included.")

    # ── Summary stats ────────────────────────────────────────
    if gm_stats:
        st.divider()
        st.subheader("League snapshot")

        sorted_gms = sorted(gm_stats.values(), key=lambda g: g["luck"])
        most_unlucky = sorted_gms[0]
        luckiest = sorted_gms[-1]
        most_apps = max(gm_stats.values(), key=lambda g: g["appearances"])
        most_titles = max(gm_stats.values(), key=lambda g: g["act_champ"])

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("GMs analysed", len(gm_stats))
        c2.metric("Most unlucky", most_unlucky["name"],
                  f"{most_unlucky['luck']:+.2f} luck delta")
        c3.metric("Luckiest", luckiest["name"],
                  f"{luckiest['luck']:+.2f} luck delta")
        c4.metric("Most appearances", most_apps["name"],
                  f"{most_apps['appearances']} playoff years")

        st.markdown("---")
        st.markdown("**Most unlucky GMs (0 titles, ≥5 appearances)**")
        no_ring = [g for g in gm_stats.values() if g["act_champ"] == 0 and g["appearances"] >= 5]
        no_ring.sort(key=lambda g: g["p_no_champ"])
        for g in no_ring[:5]:
            pct = g["p_no_champ"] * 100
            st.markdown(
                f"- **{g['name']}** ({g['appearances']} apps) — "
                f"P(0 rings) = **{pct:.1f}%**"
            )

    # ── Build HTML ────────────────────────────────────────────
    st.divider()
    st.subheader("Download dashboard")

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    gm_json = json.dumps(gm_stats, separators=(",", ":"))
    html_out = (
        template
        .replace("{{LEAGUE_NAME}}", display_name)
        .replace("{{GM_DATA}}", gm_json)
    )

    st.download_button(
        label="⬇ Download dashboard HTML",
        data=html_out.encode("utf-8"),
        file_name=f"{slug}_gm_fortune.html",
        mime="text/html",
        help="A single self-contained HTML file. Open in any browser or share the link.",
    )

    st.info(
        "The downloaded file is fully self-contained — no server needed. "
        "Open it in any browser, or host it anywhere (GitHub Pages, Dropbox public link, etc.)."
    )

# ─────────────────────────────────────────────────────────────
# Footer
# ─────────────────────────────────────────────────────────────
st.markdown("---")
st.caption(
    "Built on StatsPlus game history · "
    "Pythagorean win% (exp 1.83) · Log5 head-to-head · "
    "Binomial series probability · Actual bracket path tracing"
)
