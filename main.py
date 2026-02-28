import os
import json
import time
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Strava Dashboard API")

# ── CORS — localhost (dev) + domínio do Railway (prod) ──────────────────────
FRONTEND_URL = os.getenv("FRONTEND_URL", "")
origins = ["http://localhost:5173", "http://localhost:3000"]
if FRONTEND_URL:
    origins.append(FRONTEND_URL)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CLIENT_ID     = os.getenv("STRAVA_CLIENT_ID")
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("STRAVA_REDIRECT_URI", "http://localhost:8000/callback")

# ── Token storage ─────────────────────────────────────────────────────────────
# Em produção (Railway) não há filesystem persistente, então salva em memória.
# O token será repedido via /auth se o servidor reiniciar.
_token_cache: dict | None = None

def save_token(data: dict):
    global _token_cache
    _token_cache = data
    # também tenta salvar em arquivo (dev)
    try:
        with open(".strava_token", "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def load_token() -> dict | None:
    if _token_cache:
        return _token_cache
    if os.path.exists(".strava_token"):
        with open(".strava_token") as f:
            return json.load(f)
    return None

async def get_valid_token() -> str:
    token = load_token()
    if not token:
        raise HTTPException(status_code=401, detail="Não autenticado. Acesse /auth para autorizar.")
    if token["expires_at"] < time.time():
        async with httpx.AsyncClient() as client:
            resp = await client.post("https://www.strava.com/oauth/token", data={
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type":    "refresh_token",
                "refresh_token": token["refresh_token"],
            })
            resp.raise_for_status()
            save_token(resp.json())
            return resp.json()["access_token"]
    return token["access_token"]

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/auth")
def auth():
    url = (
        f"https://www.strava.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=read,activity:read_all"
    )
    return HTMLResponse(f"""
        <html><body style="font-family:monospace;background:#060d14;color:#f97316;padding:40px;text-align:center">
        <h2>🏃 Autenticando com Strava...</h2>
        <script>window.location.href = "{url}";</script>
        </body></html>
    """)

@app.get("/caSllback")
@app.get("/callback")
async def callback(code: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post("https://www.strava.com/oauth/token", data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code":          code,
            "grant_type":    "authorization_code",
        })
        resp.raise_for_status()
        save_token(resp.json())

    frontend = os.getenv("FRONTEND_URL", "http://localhost:5173")
    return HTMLResponse(f"""
        <html><body style="font-family:monospace;background:#060d14;color:#34d399;padding:40px;text-align:center">
        <h2>✅ Autenticado!</h2>
        <p style="color:#94a3b8">Redirecionando para o dashboard...</p>
        <script>setTimeout(() => window.location.href = "{frontend}", 1500);</script>
        </body></html>
    """)

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/athlete")
async def get_athlete():
    token = await get_valid_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get("https://www.strava.com/api/v3/athlete",
            headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.json()

@app.get("/activities")
async def get_activities(per_page: int = 50, page: int = 1):
    token = await get_valid_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get("https://www.strava.com/api/v3/athlete/activities",
            headers={"Authorization": f"Bearer {token}"},
            params={"per_page": per_page, "page": page})
        resp.raise_for_status()
    runs = []
    for a in resp.json():
        if a.get("type") not in ("Run", "VirtualRun"):
            continue
        runs.append({
            "id":        a["id"],
            "name":      a["name"],
            "date":      a["start_date_local"][:10],
            "distance":  round(a["distance"] / 1000, 2),
            "duration":  a["moving_time"],
            "pace":      fmt_pace(a["moving_time"], a["distance"]),
            "elevation": round(a.get("total_elevation_gain", 0)),
            "avgHr":     a.get("average_heartrate"),
            "maxHr":     a.get("max_heartrate"),
            "calories":  a.get("calories"),
            "polyline":  a.get("map", {}).get("summary_polyline"),
        })
    return runs

@app.get("/activities/{activity_id}/streams")
async def get_streams(activity_id: int):
    token = await get_valid_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://www.strava.com/api/v3/activities/{activity_id}/streams",
            headers={"Authorization": f"Bearer {token}"},
            params={"keys": "heartrate,velocity_smooth,time,distance,altitude", "key_by_type": "true"})
        resp.raise_for_status()
        return resp.json()

@app.get("/stats")
async def get_stats():
    token = await get_valid_token()
    athlete = await get_athlete()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://www.strava.com/api/v3/athletes/{athlete['id']}/stats",
            headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.json()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/")
def root():
    return {"endpoints": ["/auth", "/athlete", "/activities", "/activities/{id}/streams", "/stats"]}

# ── Utils ─────────────────────────────────────────────────────────────────────

def fmt_pace(seconds: int, meters: float) -> str:
    if not meters:
        return "--"
    pace_sec = (seconds / meters) * 1000
    return f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d}"

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)