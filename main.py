import os
import json
import time
import httpx
import psycopg2
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Strava Dashboard API")

# ── CORS ──────────────────────────────────────────────────────────────────────
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
DATABASE_URL  = os.getenv("DATABASE_URL")

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Cria a tabela de tokens se não existir."""
    if not DATABASE_URL:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS tokens (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        data JSONB NOT NULL,
                        updated_at TIMESTAMP DEFAULT NOW()
                    )
                """)
            conn.commit()
    except Exception as e:
        print(f"[DB] Erro ao inicializar: {e}")

# Inicializa o banco ao subir o servidor
init_db()

# ── Token storage — PostgreSQL com fallback para arquivo local ────────────────
_token_cache: dict | None = None

def save_token(data: dict):
    global _token_cache
    _token_cache = data

    # Salva no PostgreSQL (produção)
    if DATABASE_URL:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO tokens (id, data, updated_at)
                        VALUES (1, %s, NOW())
                        ON CONFLICT (id) DO UPDATE
                            SET data = EXCLUDED.data,
                                updated_at = NOW()
                    """, (json.dumps(data),))
                conn.commit()
            print("[DB] Token salvo no PostgreSQL.")
        except Exception as e:
            print(f"[DB] Erro ao salvar token: {e}")

    # Fallback: arquivo local (desenvolvimento)
    try:
        with open(".strava_token", "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def load_token() -> dict | None:
    global _token_cache

    # 1. Cache em memória (mais rápido)
    if _token_cache:
        return _token_cache

    # 2. PostgreSQL (produção)
    if DATABASE_URL:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT data FROM tokens WHERE id = 1")
                    row = cur.fetchone()
                    if row:
                        _token_cache = row[0]
                        print("[DB] Token carregado do PostgreSQL.")
                        return _token_cache
        except Exception as e:
            print(f"[DB] Erro ao carregar token: {e}")

    # 3. Arquivo local (desenvolvimento)
    if os.path.exists(".strava_token"):
        with open(".strava_token") as f:
            data = json.load(f)
            _token_cache = data
            return data

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
            new_token = resp.json()
            save_token(new_token)
            return new_token["access_token"]
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
            "avgSpeed":  round(a.get("average_speed", 0) * 3.6, 1),
            "maxSpeed":  round(a.get("max_speed", 0) * 3.6, 1),
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
    db_ok = False
    if DATABASE_URL:
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            db_ok = True
        except Exception:
            pass
    return {"status": "ok", "db": "connected" if db_ok else "unavailable"}

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
