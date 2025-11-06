from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Load environment variables early (DB, OpenAI keys, etc.)
load_dotenv()

app = FastAPI()

# --- CORS -----------------------------------------------------------------
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/")
def home():
    return {"message": "API is running. Try /health or /docs"}


@app.get("/health")
def health():
    return {"ok": True}


# --- Routers --------------------------------------------------------------
from app.routes import (
    audio,
    db_proxy,
    db_test,
    realtime,
    realtime_openai,
    realtime_ws,
    reasoning,
    snapshot,
    summary,
    tools,
    visits,
)


app.include_router(snapshot.router, prefix="/EHR", tags=["EHR"])
app.include_router(audio.router, prefix="/audio", tags=["audio"])
app.include_router(reasoning.router, prefix="/reas", tags=["reasoning"])
app.include_router(summary.router, prefix="/summary", tags=["summary"])
app.include_router(tools.router, tags=["tools"])
app.include_router(realtime.router, tags=["realtime"])
app.include_router(realtime_openai.router, tags=["realtime"])
app.include_router(realtime_ws.router)
app.include_router(db_proxy.router, tags=["database"])
app.include_router(db_test.router, tags=["database"])
app.include_router(visits.router, prefix="/visits", tags=["visits"])
