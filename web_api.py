import hashlib
import secrets
import sqlite3
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sports_provider import (
    filter_matches,
    get_data_source_label,
    get_odds_api_meta,
    list_leagues,
    load_all_matches,
    predict_match,
    refresh_from_odds_api,
)

DB_PATH = Path(__file__).resolve().parent / "members.db"
BASE_DIR = Path(__file__).resolve().parent
MATCH_CACHE: List[dict] = []


def hash_password(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                nickname TEXT NOT NULL,
                phone TEXT,
                role TEXT NOT NULL CHECK(role IN ('admin', 'member')),
                created_by INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        conn.commit()

    seed_admin("master01", "master123456", "總代理A")
    seed_admin("engineer01", "0912345678", "工程師")
    seed_admin("admin01", "admin123456", "超級管理員A")
    seed_admin("admin02", "admin123456", "超級管理員B")


def can_export_import(user: sqlite3.Row) -> bool:
    return user["username"] in {"master01", "engineer01"}


def is_master(user: sqlite3.Row) -> bool:
    return user["username"] == "master01"


def can_manage_members(user: sqlite3.Row) -> bool:
    return user["role"] == "admin" or is_master(user)


def seed_admin(username: str, password: str, nickname: str):
    with db_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row:
            return
        conn.execute(
            """
            INSERT INTO users (username, password_hash, nickname, phone, role)
            VALUES (?, ?, ?, ?, 'admin')
            """,
            (username, hash_password(password), nickname, ""),
        )
        conn.commit()


def seed_user_hashed(username: str, nickname: str, role: str, password_hash: str):
    with db_conn() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row:
            return
        conn.execute(
            """
            INSERT INTO users (username, password_hash, nickname, phone, role)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, password_hash, nickname, "", role),
        )
        conn.commit()


def parse_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="未登入")
    parts = authorization.strip().split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Token 格式錯誤")
    return parts[1]


def get_current_user(authorization: Optional[str]) -> sqlite3.Row:
    token = parse_bearer_token(authorization)
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.username, u.nickname, u.phone, u.role
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ?
            """,
            (token,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="登入已失效，請重新登入")
    return row


def require_login(authorization: Optional[str]) -> sqlite3.Row:
    return get_current_user(authorization)


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateMemberRequest(BaseModel):
    nickname: str
    username: str
    password: str


class UpdateMemberRequest(BaseModel):
    nickname: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


class ImportUserItem(BaseModel):
    username: str
    nickname: str
    role: str
    password_hash: str
    phone: Optional[str] = ""


class ImportDataRequest(BaseModel):
    users: List[ImportUserItem]


def refresh_match_cache():
    global MATCH_CACHE
    # 只讀快取，不在啟動時自動打 API（Render 休眠重啟會反覆扣 API 額度）
    MATCH_CACHE = load_all_matches()


init_db()
refresh_match_cache()

app = FastAPI(title="Titanium Sports API")
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return FileResponse(BASE_DIR / "web_index.html")


@app.get("/health")
def health():
    return {"ok": True, "service": "sports"}


@app.post("/auth/login")
def login(payload: LoginRequest):
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT id, username, nickname, phone, role, password_hash
            FROM users
            WHERE username = ?
            """,
            (payload.username.strip(),),
        ).fetchone()
        if not row or row["password_hash"] != hash_password(payload.password):
            raise HTTPException(status_code=401, detail="帳號或密碼錯誤")

        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, row["id"]))
        conn.commit()

    return {
        "token": token,
        "user": {
            "id": row["id"],
            "username": row["username"],
            "nickname": row["nickname"],
            "phone": row["phone"],
            "role": row["role"],
        },
    }


@app.post("/auth/logout")
def logout(authorization: Optional[str] = Header(default=None)):
    token = parse_bearer_token(authorization)
    with db_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    return {"ok": True}


@app.get("/auth/me")
def me(authorization: Optional[str] = Header(default=None)):
    user = get_current_user(authorization)
    return {
        "id": user["id"],
        "username": user["username"],
        "nickname": user["nickname"],
        "phone": user["phone"],
        "role": user["role"],
        "canManage": can_manage_members(user),
        "canDataOps": can_export_import(user),
    }


@app.get("/api/meta")
def api_meta(authorization: Optional[str] = Header(default=None)):
    require_login(authorization)
    sports = sorted({m["sport"] for m in MATCH_CACHE})
    return {
        "sports": sports,
        "source": get_data_source_label(MATCH_CACHE),
        "oddsApi": get_odds_api_meta(),
        "count": len(MATCH_CACHE),
    }


@app.get("/api/leagues")
def api_leagues(
    sport: str = Query(default="all"),
    authorization: Optional[str] = Header(default=None),
):
    require_login(authorization)
    return {"leagues": list_leagues(MATCH_CACHE, sport)}


@app.get("/api/matches")
def api_matches(
    sport: str = Query(default="all"),
    league: str = Query(default="all"),
    date: str = Query(default=""),
    status: str = Query(default=""),
    authorization: Optional[str] = Header(default=None),
):
    require_login(authorization)
    statuses = [s.strip() for s in status.split(",") if s.strip()] if status else []
    rows = filter_matches(MATCH_CACHE, sport=sport, league=league, day=date, statuses=statuses or None)
    return {
        "matches": rows,
        "source": get_data_source_label(MATCH_CACHE),
        "count": len(rows),
    }


@app.post("/api/refresh")
def api_refresh(
    force: bool = Query(default=False),
    authorization: Optional[str] = Header(default=None),
):
    user = require_login(authorization)
    if not can_manage_members(user):
        raise HTTPException(status_code=403, detail="只有管理員可以更新賽事資料")
    result = refresh_from_odds_api(force=force)
    global MATCH_CACHE
    if result.get("matches"):
        MATCH_CACHE = result["matches"]
    detail = "；".join(result["errors"]) if result["errors"] else ""
    if not result["ok"]:
        raise HTTPException(
            status_code=400 if "ODDS_API_KEY" in detail else 502,
            detail=detail or "未取得賽事，請稍後再試",
        )
    return result


@app.get("/api/predict/{match_id}")
def api_predict(match_id: str, authorization: Optional[str] = Header(default=None)):
    require_login(authorization)
    match = next((m for m in MATCH_CACHE if m["id"] == match_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="找不到賽事")
    return {"match": match, "prediction": predict_match(match)}


@app.get("/admin/members")
def list_members(authorization: Optional[str] = Header(default=None)):
    user = get_current_user(authorization)
    if not can_manage_members(user):
        raise HTTPException(status_code=403, detail="只有管理員可以查看會員")
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, username, nickname, phone, role, created_at
            FROM users
            ORDER BY id DESC
            """
        ).fetchall()
    return [
        {
            "id": r["id"],
            "username": r["username"],
            "nickname": r["nickname"],
            "phone": r["phone"],
            "role": r["role"],
            "createdAt": r["created_at"],
        }
        for r in rows
    ]


@app.post("/admin/members")
def create_member(payload: CreateMemberRequest, authorization: Optional[str] = Header(default=None)):
    user = get_current_user(authorization)
    if not can_manage_members(user):
        raise HTTPException(status_code=403, detail="只有管理員可以建立會員")

    username = payload.username.strip()
    nickname = payload.nickname.strip()
    password = payload.password
    if not username or not nickname or not password:
        raise HTTPException(status_code=400, detail="帳號、暱稱、密碼不可空白")

    with db_conn() as conn:
        exists = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if exists:
            raise HTTPException(status_code=400, detail="帳號已存在")
        conn.execute(
            """
            INSERT INTO users (username, password_hash, nickname, phone, role, created_by)
            VALUES (?, ?, ?, ?, 'member', ?)
            """,
            (username, hash_password(password), nickname, "", user["id"]),
        )
        conn.commit()
    return {"ok": True}


@app.put("/admin/members/{member_id}")
def update_member(
    member_id: int,
    payload: UpdateMemberRequest,
    authorization: Optional[str] = Header(default=None),
):
    user = get_current_user(authorization)
    if not can_manage_members(user):
        raise HTTPException(status_code=403, detail="只有管理員可以編輯會員")

    with db_conn() as conn:
        target = conn.execute(
            "SELECT id, role, username, nickname FROM users WHERE id = ?",
            (member_id,),
        ).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="找不到會員")

        new_username = (payload.username or target["username"]).strip()
        new_nickname = (payload.nickname or target["nickname"]).strip()
        if not new_username or not new_nickname:
            raise HTTPException(status_code=400, detail="帳號與暱稱不可空白")

        exists = conn.execute(
            "SELECT id FROM users WHERE username = ? AND id != ?",
            (new_username, member_id),
        ).fetchone()
        if exists:
            raise HTTPException(status_code=400, detail="帳號已存在")

        if payload.password:
            conn.execute(
                """
                UPDATE users
                SET username = ?, nickname = ?, password_hash = ?
                WHERE id = ?
                """,
                (new_username, new_nickname, hash_password(payload.password), member_id),
            )
        else:
            conn.execute(
                """
                UPDATE users
                SET username = ?, nickname = ?
                WHERE id = ?
                """,
                (new_username, new_nickname, member_id),
            )
        conn.commit()
    return {"ok": True}


@app.delete("/admin/members/{member_id}")
def delete_member(member_id: int, authorization: Optional[str] = Header(default=None)):
    user = get_current_user(authorization)
    if not can_manage_members(user):
        raise HTTPException(status_code=403, detail="只有管理員可以刪除會員")
    if user["id"] == member_id:
        raise HTTPException(status_code=400, detail="不能刪除目前登入中的管理員")

    with db_conn() as conn:
        target = conn.execute("SELECT id FROM users WHERE id = ?", (member_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="找不到會員")
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (member_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (member_id,))
        conn.commit()
    return {"ok": True}


@app.get("/admin/export-data")
def export_data(authorization: Optional[str] = Header(default=None)):
    user = get_current_user(authorization)
    if not can_export_import(user):
        raise HTTPException(status_code=403, detail="只有總代理與工程師可以匯出資料")
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT username, nickname, role, password_hash, phone
            FROM users
            ORDER BY id ASC
            """
        ).fetchall()
    return {
        "users": [
            {
                "username": r["username"],
                "nickname": r["nickname"],
                "role": r["role"],
                "password_hash": r["password_hash"],
                "phone": r["phone"] or "",
            }
            for r in rows
        ]
    }


@app.post("/admin/import-data")
def import_data(payload: ImportDataRequest, authorization: Optional[str] = Header(default=None)):
    user = get_current_user(authorization)
    if not can_export_import(user):
        raise HTTPException(status_code=403, detail="只有總代理與工程師可以匯入資料")
    if not payload.users:
        raise HTTPException(status_code=400, detail="匯入資料不可為空")

    allowed_roles = {"admin", "member"}
    with db_conn() as conn:
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM users")
        for u in payload.users:
            role = u.role.strip().lower()
            if role not in allowed_roles:
                raise HTTPException(status_code=400, detail=f"不支援的角色: {u.role}")
            conn.execute(
                """
                INSERT INTO users (username, password_hash, nickname, phone, role)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    u.username.strip(),
                    u.password_hash.strip(),
                    u.nickname.strip(),
                    (u.phone or "").strip(),
                    role,
                ),
            )
        conn.commit()
    init_db()
    return {"ok": True, "count": len(payload.users)}
