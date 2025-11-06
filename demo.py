# demo.py
# ------------------------------------------------------------
# Unified Streamlit UI for AI-Triage-Homie Summary Agent (MVP)
# - Patient & Doctor IDs (snapshot on /start)
# - Doctor Notes panel (/summary/message)
# - Chat input that hits /summary/run (reply + orchestration)
# - Objective & SOAP panels rendered once (unique keys)
# ------------------------------------------------------------

import streamlit as st
import requests
from datetime import datetime

API = "http://127.0.0.1:8000/summary"

st.set_page_config(page_title="AI-Triage-Homie", layout="wide", page_icon="🩺")
st.title("🩺 AI-Triage-Homie — Doctor Summary Agent")

# ------------------------------------------------------------
# Session persistence
# ------------------------------------------------------------
if "session_id" not in st.session_state:
    st.session_state.session_id = None
if "transcript" not in st.session_state:
    st.session_state.transcript = []
if "objective" not in st.session_state:
    st.session_state.objective = ""
if "soap" not in st.session_state:
    st.session_state.soap = ""

# ------------------------------------------------------------
# Layout sections
# ------------------------------------------------------------
col_chat, col_obs, col_soap = st.columns([2, 1, 1])
with col_chat:
    st.subheader("Conversation Transcript")
    transcript_box = st.empty()

with col_obs:
    st.subheader("Observation (Objective)")
    obs_box = st.empty()

with col_soap:
    st.subheader("Final SOAP Summary")
    soap_box = st.empty()

st.divider()

# ------------------------------------------------------------
# Controls / Setup
# ------------------------------------------------------------
st.markdown("### Session Controls")

col_inputs = st.columns([1, 1, 1, 2])
patient_id = col_inputs[0].number_input("Patient ID", min_value=1, value=101)
doctor_id = col_inputs[1].text_input("Doctor ID", value="dr_demo")
locale = col_inputs[2].selectbox("Language", ["en", "fr", "ar"], index=0)
consent = True

c1, c2 = st.columns([1, 1])
start_btn = c1.button("▶️ Start Session")
end_btn = c2.button("⏹️ End Session")

def append_message(role, text):
    st.session_state.transcript.append(
        {"role": role, "text": text, "time": datetime.now().strftime("%H:%M:%S")}
    )

def refresh_transcript():
    chat = "\n".join(
        [
            f"**{'🧑‍⚕️ Doctor' if t['role']=='doctor' else '🤖 Assistant'} ({t['time']}):** {t['text']}"
            for t in st.session_state.transcript
        ]
    )
    transcript_box.markdown(chat or "_No messages yet._")

def reset_session_state():
    st.session_state.session_id = None
    st.session_state.transcript.clear()
    st.session_state.objective = ""
    st.session_state.soap = ""
    refresh_transcript()
    obs_box.empty()
    soap_box.empty()

# Start session → /summary/start
if start_btn:
    payload = {
        "patient_id": patient_id,
        "doctor_id": doctor_id,
        "locale": locale,
        "consent": consent,
    }
    try:
        r = requests.post(f"{API}/start", json=payload)
        if r.status_code == 200:
            js = r.json()
            st.session_state.session_id = js["session_id"]
            append_message("assistant", "Session initialized. You may begin speaking.")
            st.success(f"✅ Session started (Patient {patient_id})")
        else:
            st.error(f"❌ Failed: {r.text}")
    except Exception as e:
        st.error(f"Error contacting backend: {e}")

# End session (local reset only)
if end_btn:
    reset_session_state()
    st.warning("Session ended.")

sid = st.session_state.session_id
if not sid:
    st.stop()

st.markdown(f"**Session ID:** `{sid}`")

# ------------------------------------------------------------
# Doctor Notes (stored, not triggering reasoning) → /summary/message
# ------------------------------------------------------------
st.markdown("### 📝 Doctor Notes (stored but not triggering reasoning)")
note_text = st.text_area("Enter case notes here...", height=100, key="note_box")

if st.button("💾 Save Note"):
    if not note_text.strip():
        st.warning("Please enter some text before saving.")
    else:
        try:
            r = requests.post(f"{API}/message", json={"session_id": sid, "text": note_text})
            if r.status_code == 200:
                js = r.json()
                st.success(f"Note saved (total notes: {js['total_messages']})")
                append_message("doctor", f"[Note] {note_text}")
                refresh_transcript()
            else:
                st.error(f"Failed to save note: {r.text}")
        except Exception as e:
            st.error(f"Failed to contact backend: {e}")

st.divider()

# ------------------------------------------------------------
# Conversation (chat or speech) → /summary/run
# ------------------------------------------------------------
user_input = st.chat_input("Speak or type your message to the assistant...")

if user_input:
    append_message("doctor", user_input)
    refresh_transcript()

    try:
        r = requests.post(f"{API}/run", json={"session_id": sid, "text": user_input})
        if r.status_code == 200:
            js = r.json()

            # Assistant speech (always)
            agent_reply = js.get("speech_output", "")
            append_message("assistant", agent_reply)

            # Update UI state ONLY (do not render here to avoid duplicate widget IDs)
            ui = js.get("ui") or {}
            intent = js.get("intent", "")
            if "objective" in ui:
                st.session_state.objective = ui["objective"]
            if "soap" in ui:
                st.session_state.soap = ui["soap"]

            # Optional toast cue
            if intent in ["objective", "finalize", "show_preview"]:
                st.toast(f"{intent.upper()} preview generated.", icon="📄")

            refresh_transcript()
        else:
            st.error(f"Backend error: {r.text}")
    except Exception as e:
        st.error(f"Failed to contact backend: {e}")

# ------------------------------------------------------------
# Render preview panels once (unique keys avoid duplicate ID errors)
# ------------------------------------------------------------
refresh_transcript()

if st.session_state.objective:
    obs_box.text_area(
        "Observation",
        value=st.session_state.objective,
        height=250,
        key="objective_view",  # unique key
    )

if st.session_state.soap:
    soap_box.text_area(
        "SOAP",
        value=str(st.session_state.soap),
        height=250,
        key="soap_view",  # unique key
    )
