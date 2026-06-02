# app.py
"""Minimal Streamlit control panel for OpsPilot.

- Dark‑theme by default (use Streamlit's built‑in theme settings).
- Sidebar: system configuration (e.g., chunk size).
- Main area: on‑demand queries and manual pipeline trigger.

All API calls use the live backend URL:
https://opspilot-904782447299.us-central1.run.app
"""

import os
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BACKEND_URL = "https://opspilot-904782447299.us-central1.run.app"

# ---------------------------------------------------------------------------
# Streamlit page setup
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="OpsPilot Control Panel",
    page_icon=":gear:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Force dark theme if the user hasn't set a theme – Streamlit respects the user's
# config file, but we can hint at a dark background via a simple CSS tweak that
# does not add decorative content.
st.markdown(
    """
    <style>
    .stApp { background-color: #0e1117; color: #fafafa; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Sidebar – System Configuration
# ---------------------------------------------------------------------------
st.sidebar.header("System Configuration")
chunk_size = st.sidebar.slider("Chunk Size", min_value=100, max_value=1000, value=500, step=10)
if st.sidebar.button("Update Configuration"):
    payload = {"chunk_size": chunk_size}
    try:
        resp = requests.post(f"{BACKEND_URL}/config", json=payload, timeout=10)
        resp.raise_for_status()
        st.sidebar.success("Configuration updated successfully")
    except Exception as e:
        st.sidebar.error(f"Failed to update config: {e}")

# ---------------------------------------------------------------------------
# Main area – On‑Demand Queries
# ---------------------------------------------------------------------------
st.header("OpsPilot Query Interface")
query_text = st.text_input("Ask OpsPilot…")
if st.button("Submit Query"):
    if query_text:
        try:
            resp = requests.post(
                f"{BACKEND_URL}/query", json={"query": query_text}, timeout=15
            )
            resp.raise_for_status()
            with st.expander("Response", expanded=True):
                st.json(resp.json())
        except Exception as e:
            st.error(f"Query failed: {e}")
    else:
        st.warning("Please enter a query before submitting.")

# ---------------------------------------------------------------------------
# Main area – Pipeline Execution
# ---------------------------------------------------------------------------
st.header("Manual Operations Briefing")
if st.button("🚀 Trigger Manual Operations Briefing"):
    with st.spinner("Executing 4‑Node LangGraph Pipeline..."):
        try:
            resp = requests.post(
                f"{BACKEND_URL}/briefing/trigger", timeout=30
            )
            resp.raise_for_status()
            with st.expander("Briefing Result", expanded=False):
                st.json(resp.json())
        except Exception as e:
            st.error(f"Briefing failed: {e}")
