🩺 AI-Triage-Homie — Summary Agent MVP
🚀 Overview

AI-Triage-Homie is a FastAPI-based, speech-enabled medical triage and summarization assistant.
This MVP implements the Summary Agent — a doctor-facing module designed to:

Capture and manage patient visit sessions

Pull structured data (EHR snapshot)

Conduct guided, question-driven reasoning using GPT models

Generate structured Objective (Observation) and SOAP summaries

Integrate later with speech input/output and Khumar’s Differential Diagnosis agent

🧱 System Architecture
Doctor (Streamlit UI)
     ↓
FastAPI Backend (/summary)
     ├── /start         → creates session + builds patient snapshot
     ├── /message       → logs doctor notes
     ├── /run           → orchestrates reasoning & intent routing
     ├── /objective     → generates and saves Objective section
     ├── /finalize      → generates & saves SOAP notes
     ↓
Reasoning Client (GPT models)
     ↓
In-Memory Session Store → EHR Database (Zara’s write layer)

🧩 Key Components
File	Description
app/main.py	Initializes FastAPI, mounts routes, enables CORS
app/routes/summary.py	Main endpoints for session management, reasoning, and saving
app/services/summary_session.py	In-memory session storage
app/services/snapshot_builder.py	Builds a compact patient data snapshot from EHR tables
app/clients/reasoning_client.py	Interfaces GPT models and validates structured JSON output
demo.py	Streamlit-based UI for the Summary Agent (doctor interface)
💡 Features

✅ Doctor can start new patient sessions
✅ Snapshot auto-fetched from EHR mock tables
✅ Notes and conversation tracked in real-time
✅ GPT-powered reasoning agent actively asks clarifying questions
✅ Objective and SOAP summaries generated when confidence rises
✅ Ready for full speech-to-speech integration (Stacy’s ASR/TTS modules)
✅ Writes approved summaries back to EHR (Zara’s visit writer service)

⚙️ Setup Instructions
1️⃣ Clone the Repository
git clone https://github.com/<your-username>/AI-Triage-Homie.git
cd AI-Triage-Homie

2️⃣ Create & Activate Virtual Environment
python -m venv .venv
source .venv/bin/activate     # Mac/Linux
.venv\Scripts\activate        # Windows

3️⃣ Install Dependencies
pip install -r requirements.txt

4️⃣ Configure Environment Variables

Create a .env file in the root:

E_HOSPITAL_BASE_URL=http://127.0.0.1:8000
OPENAI_API_KEY=your_openai_key_here

5️⃣ Run the Backend
uvicorn app.main:app --reload


Backend available at:
👉 http://127.0.0.1:8000/docs

6️⃣ Run the Streamlit UI

In a new terminal window:

streamlit run demo.py


Access the UI at:
👉 http://localhost:8501

🧠 Usage Flow

1️⃣ Start Session — provide patient ID and doctor ID.
2️⃣ Add Notes — type in relevant observations or context.
3️⃣ Chat / Speak — converse naturally; agent asks questions to uncover missing details.
4️⃣ Objective Preview — agent auto-generates Objective when confident.
5️⃣ SOAP Finalization — once confirmed, full SOAP summary generated and stored.


🧠 Next Steps

🔊 Integrate speech-to-speech via asr.py + tts.py

🧩 Connect live EHR database tables

🧠 Add orchestrator-driven confidence-based intent switching (Objective ↔ Finalize)

🧾 Expand structured JSON output validation and saving pipeline

🧪 Demo Walkthrough

Below is a scripted walkthrough you can follow to test or demo the Summary Agent end-to-end.

🩺 Scenario

Patient: 103
Doctor: Dr. Hassan

Step 1 – Start Session

In Streamlit, enter:

Patient ID: 103
Doctor ID: dr_hassan


Click Start Session.

Step 2 – Add Notes

Under “Doctor Notes,” enter:

Patient presented with high fever, fatigue, and muscle aches for two days.
Tylenol provided minimal relief. No recent travel or exposures.
Slight sore throat and mild headache, no rash or respiratory distress.
History of moderate diabetes and mild peanut allergy. No labs or imaging performed yet.


Click Save Note.

Step 3 – Begin Conversation

In the chat input, type:

I’d like to discuss a patient with high fever and fatigue.


The agent should begin asking clarifying questions (e.g., duration, associated symptoms).

Step 4 – Provide Details

Answer the agent’s questions naturally:

The fever started two days ago and has been continuous.

He took Tylenol, but it didn’t help much.

He has a mild sore throat and headache, no rash or breathing issues.

Step 5 – Confidence Rises

When the agent’s confidence increases, it will respond:

“I believe we’ve gathered most of the relevant details. Would you like to preview the Objective or SOAP summary?”

At that point, you can simply reply:

Yes, please show me the Objective section.


The Observation (Objective) box on the right should populate automatically.

Step 6 – Finalize SOAP

Once satisfied, say:

Let’s finalize the summary.


The Final SOAP Summary box will appear, ready for review and saving.

🪶 License

MIT License — open for educational and research purposes.