# streamlit_app.py
"""
Streamlit UI for OpsPilot configuration management.
Provides a clean, button‑and‑input interface for non‑technical users to view and update
the `/config` endpoints of the OpsPilot FastAPI backend.
"""

import os
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Configuration – adjust via environment when deploying to Streamlit Community Cloud
# ---------------------------------------------------------------------------
BASE_URL = os.getenv(
    "OPS_PILOT_URL", "https://opspilot-904782447299.us-central1.run.app"
)
DEFAULT_API_KEY = os.getenv("OPS_PILOT_API_KEY", "")

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def _auth_headers(api_key: str) -> dict:
    """Return headers with the required ``X-API-KEY`` if provided."""
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-KEY"] = api_key
    return headers

def get_config(api_key: str) -> dict:
    """Fetch current configuration from the OpsPilot backend."""
    resp = requests.get(
        f"{BASE_URL}/api/v1/config", headers=_auth_headers(api_key), timeout=10
    )
    resp.raise_for_status()
    return resp.json()

def update_config(api_key: str, payload: dict) -> dict:
    """Send a configuration update request to the backend."""
    resp = requests.post(
        f"{BASE_URL}/api/v1/config",
        json=payload,
        headers={**_auth_headers(api_key), "Content-Type": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()

# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------
st.set_page_config(page_title="OpsPilot Config", page_icon=":gear:", layout="centered")
st.title("⚙️ OpsPilot Configuration Dashboard")

# API key entry – can be pre‑filled from env for trusted deployments
api_key = st.text_input("API Key", value=DEFAULT_API_KEY, type="password")
if not api_key:
    st.warning("Please provide the API key to interact with the protected endpoints.")
    st.stop()

# ---------------------------------------------------------------------------
# Load and display current configuration
# ---------------------------------------------------------------------------
with st.spinner("Loading current configuration …"):
    try:
        cfg = get_config(api_key)
    except Exception as e:
        st.error(f"Failed to load configuration: {e}")
        st.stop()

st.subheader("Current Settings")
# Show values in editable fields – only expose the fields that the API allows to update.
st.number_input(
    "Stale Ticket Days",
    min_value=1,
    max_value=90,
    key="stale_ticket_days",
    value=cfg.get("stale_ticket_days", 5),
)
st.number_input(
    "Stale PR Days",
    min_value=1,
    max_value=60,
    key="stale_pr_days",
    value=cfg.get("stale_pr_days", 4),
)
st.number_input(
    "Stale Notion Doc Days",
    min_value=1,
    max_value=365,
    key="stale_notion_doc_days",
    value=cfg.get("stale_notion_doc_days", 30),
)
st.text_input(
    "Slack Channel ID",
    key="slack_channel_id",
    value=cfg.get("slack_channel_id", ""),
)

st.markdown("---")
if st.button("💾 Save Changes"):
    payload = {
        "stale_ticket_days": st.session_state["stale_ticket_days"],
        "stale_pr_days": st.session_state["stale_pr_days"],
        "stale_notion_doc_days": st.session_state["stale_notion_doc_days"],
        "slack_channel_id": st.session_state["slack_channel_id"],
    }
    with st.spinner("Updating configuration …"):
        try:
            result = update_config(api_key, payload)
            st.success("Configuration updated successfully!")
            st.json(result)
        except Exception as e:
            st.error(f"Update failed: {e}")

st.caption(
    "*Changes affect the running process only. To persist them across restarts, update the `.env` file on the server and redeploy."
)
