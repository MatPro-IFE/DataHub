# SPDX-FileCopyrightText: 2026 Institutt for Energiteknikk (IFE)
#
# SPDX-License-Identifier: Apache-2.0

"""
DataHub dashboard - FastAPI web application and session API.

All project-specific behaviour (dataset types, parameters, file classes,
quantities of interest) comes from the project schema (config/schema.yaml).

Pages
  /                         dataset search          /dataset/{id}     dataset detail
  /upload                   folder / file upload    /schema           active project schema
  /graph                    knowledge-graph explorer
  /sweeps, /sweeps/{id}     parameter sweeps        /admin/...        users, sessions, downloads
JSON API (session cookie)
  GET  /api/schema                       GET  /api/datasets?q=&type=&tag=&param=&op=&value=
  GET  /api/datasets/{id}                DELETE /api/datasets/{id}
  POST /api/upload/start | file | commit | abort
  GET  /api/download/{file_id}           GET  /api/datasets/{id}/archive.zip
  GET  /api/similar/params/{id}          GET  /api/similar/fields/{id}?field=
  POST /api/embed/{id}                   GET  /api/graph/overview, /api/graph/neighbors/{id}
  GET/POST /api/sweeps ...               GET  /health
"""
from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import uuid as uuid_module
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
import requests as _requests
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from urllib.parse import quote as _urlquote
from fastapi.templating import Jinja2Templates
from minio import Minio
from passlib.context import CryptContext
from starlette.middleware.sessions import SessionMiddleware

from datahub_sdk.schema import SchemaError, load_schema

from dashboard.ingest import IngestError, delete_dataset, ingest_folder

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Configuration from environment ───────────────────────────────

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS   = os.environ.get("MINIO_ACCESS_KEY", "")
MINIO_SECRET   = os.environ.get("MINIO_SECRET_KEY", "")
MINIO_BUCKET   = os.environ.get("MINIO_BUCKET", "datahub-data")
MINIO_SECURE   = os.environ.get("MINIO_SECURE", "false").lower() == "true"
SESSION_SECRET = os.environ.get("DASHBOARD_SECRET_KEY", "")
if len(SESSION_SECRET) < 32:
    raise RuntimeError("DASHBOARD_SECRET_KEY must be set (at least 32 characters).")
ADMIN_PASSWORD = os.environ.get("DATAHUB_ADMIN_PASSWORD", "")
ML_API_URL     = os.environ.get("ML_API_URL", "http://ml-api:8090")
PUBLIC_URL_HOST = __import__("urllib.parse").parse.urlsplit(os.environ.get("DATAHUB_PUBLIC_URL", "")).netloc
UPLOAD_DIR     = Path(os.environ.get("DATAHUB_UPLOAD_DIR", tempfile.gettempdir())) / "datahub-uploads"

try:
    SCHEMA = load_schema()
except SchemaError as _e:
    raise RuntimeError(f"Project schema error: {_e}") from _e

# DB connections return TIMESTAMPTZ in the display time zone (DASHBOARD_TZ, default UTC).
_TZ = os.environ.get("DASHBOARD_TZ", "UTC")
_DB_CONNECT = dict(
    host=os.environ.get("POSTGRES_HOST", "db"),
    port=int(os.environ.get("POSTGRES_PORT", "5432")),
    dbname=os.environ.get("POSTGRES_DB", "postgres"),
    user=os.environ.get("POSTGRES_USER_DB", "postgres"),
    password=os.environ.get("POSTGRES_PASSWORD", ""),
    options=f"-c timezone={_TZ}",
)

# ── Password hashing ──────────────────────────────────────────────

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


# ── Bootstrap admin user on first start ──────────────────────────

def _bootstrap_admin() -> bool:
    """Create the default admin account if no users exist yet. Returns True on success."""
    if not ADMIN_PASSWORD:
        logger.error("DATAHUB_ADMIN_PASSWORD is not set; no bootstrap admin created.")
        return True
    try:
        conn = _get_db_conn()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM datahub_users")
        count = cur.fetchone()["n"]
        if count == 0:
            cur.execute(
                """INSERT INTO datahub_users (username, email, password_hash, role, must_change_password)
                   VALUES (%s, %s, %s, 'admin', TRUE) ON CONFLICT (username) DO NOTHING""",
                ("admin", None, hash_password(ADMIN_PASSWORD)),
            )
            conn.commit()
            logger.info("DataHub: created bootstrap user 'admin' (password from DATAHUB_ADMIN_PASSWORD; "
                        "a password change is required at first login).")
        conn.close()
        return True
    except Exception as exc:
        logger.warning("Bootstrap admin creation failed: %s", exc)
        return False


def _ensure_bucket() -> bool:
    """Create the object-store bucket on first start (works with any S3-compatible store)."""
    try:
        mc = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS, secret_key=MINIO_SECRET, secure=MINIO_SECURE)
        if not mc.bucket_exists(MINIO_BUCKET):
            mc.make_bucket(MINIO_BUCKET)
            logger.info("DataHub: created bucket %s", MINIO_BUCKET)
        return True
    except Exception as exc:
        logger.warning("Bucket check failed: %s", exc)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    import time
    for attempt in range(30):
        ok_admin = _bootstrap_admin()
        ok_bucket = _ensure_bucket()
        if ok_admin and ok_bucket:
            break
        time.sleep(2)
    yield


# ── App & templates ───────────────────────────────────────────────

BASE_DIR  = Path(__file__).parent
# Interactive API documentation is served only to logged-in users (see /api/docs below).
app       = FastAPI(title="DataHub", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["tojson"] = lambda v, **kw: json.dumps(v, default=str, **kw)

def _fmt_localdt(dt, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format a datetime object; DB connections are set to DASHBOARD_TZ so
    the value is already in local time — just format it."""
    if dt is None:
        return "—"
    return dt.strftime(fmt)

templates.env.filters["localdt"] = _fmt_localdt

# DATAHUB_COOKIE_SECURE=true once the dashboard is served over HTTPS (reverse proxy):
# the session cookie is then only sent over TLS and HSTS is announced.
COOKIE_SECURE = os.environ.get("DATAHUB_COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
SESSION_HOURS = 8
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    max_age=SESSION_HOURS * 3600,
    same_site="lax",           # browsers do not send the cookie on cross-site POSTs
    https_only=COOKIE_SECURE,
)


# ── Access-log redaction ────────────────────────────────────────────
# Sharing-link tokens are credentials; keep them out of the access log.
_TOKEN_IN_PATH = re.compile(r"(/s/)[A-Za-z0-9_-]{8,}")


class _RedactTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_TOKEN_IN_PATH.sub(r"\1<redacted>", a) if isinstance(a, str) else a
                                for a in record.args)
        if isinstance(record.msg, str):
            record.msg = _TOKEN_IN_PATH.sub(r"\1<redacted>", record.msg)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactTokens())


def _content_disposition(filename: str) -> str:
    """RFC 6266 attachment header that survives quotes, control and non-ASCII characters."""
    name = (filename or "download").replace("\r", "").replace("\n", "")
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\;' else "_" for c in name) or "download"
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{_urlquote(name, safe="")}'


# ── CSRF defence in depth: same-origin check for state-changing requests ──
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@app.middleware("http")
async def same_origin_middleware(request: Request, call_next):
    """Reject cross-site POST/PUT/PATCH/DELETE. SameSite=Lax already keeps the session
    cookie off such requests; this also covers older browsers and same-site subdomains."""
    if request.method not in _SAFE_METHODS:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            from urllib.parse import urlsplit
            o = urlsplit(origin)
            allowed = {request.headers.get("host", "")}
            if PUBLIC_URL_HOST:
                allowed.add(PUBLIC_URL_HOST)
            fwd = request.headers.get("x-forwarded-host")
            if fwd:
                allowed.add(fwd.split(",")[0].strip())
            if o.netloc and o.netloc not in allowed:
                return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
    return await call_next(request)

# ── Security HTTP response headers ──────────────────────────────────
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.url.path.startswith("/s/"):
        # sharing-link pages: never leak the token through Referer, keep them out of search engines
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Cache-Control"] = "no-store"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    # All scripts, styles and fonts are served by the dashboard itself (dashboard/static/vendor);
    # no third-party origins. 'unsafe-inline' remains for the inline scripts of the templates.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none';"
    )
    return response

# ── Login rate limiting (in-memory, per-IP + per-username) ──────────
import time as _time
_login_failures: dict[str, list[float]] = {}  # key -> list of fail timestamps
_login_lock = threading.Lock()
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_S     = 300   # 5-minute sliding window
_LOGIN_LOCKOUT_S    = 600   # 10-minute lockout after max attempts

def _check_login_rate(key: str) -> bool:
    """Return True if the key is currently rate-limited."""
    now = _time.monotonic()
    with _login_lock:
        timestamps = _login_failures.get(key, [])
        # Keep only timestamps within the window
        timestamps = [t for t in timestamps if now - t < _LOGIN_WINDOW_S]
        _login_failures[key] = timestamps
        return len(timestamps) >= _LOGIN_MAX_ATTEMPTS

def _record_login_failure(key: str) -> None:
    now = _time.monotonic()
    with _login_lock:
        timestamps = _login_failures.get(key, [])
        timestamps = [t for t in timestamps if now - t < _LOGIN_WINDOW_S]
        timestamps.append(now)
        _login_failures[key] = timestamps

def _clear_login_failures(key: str) -> None:
    with _login_lock:
        _login_failures.pop(key, None)


# ── Heartbeat middleware ───────────────────────────────────────────
# On every HTML page request for an authenticated user, update last_seen_at
# in datahub_users.  This lets /admin/stats show who is genuinely online
# without relying solely on explicit login events.
_SKIP_HEARTBEAT = frozenset({
    "/health", "/login", "/logout", "/favicon.ico",
    "/api/docs", "/openapi.json",
})

@app.middleware("http")
async def heartbeat_middleware(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    # Only fire for non-API HTML routes (avoids hammering DB on every JS/asset request)
    if (
        not any(path.startswith(p) for p in ("/api/", "/static/"))
        and path not in _SKIP_HEARTBEAT
    ):
        uid = request.session.get("user_id") if "session" in request.scope else None
        if uid:
            try:
                conn = _get_db_conn()
                conn.cursor().execute(
                    "UPDATE datahub_users SET last_seen_at = NOW() WHERE id = %s",
                    (uid,),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass
    return response


# ── Auth helpers ──────────────────────────────────────────────────

def _get_user_by_id(user_id: str, session_id: Optional[str] = None) -> Optional[dict]:
    """User row with group memberships. With session_id, the server-side session row must
    also be open, so that logout, password changes and admin actions end sessions at once."""
    try:
        conn = _get_db_conn()
        cur  = conn.cursor()
        cur.execute(
            """SELECT u.id, u.username, u.email, u.role, u.is_active, u.organization, u.must_change_password,
                      %s::text AS session_id,
                      coalesce((SELECT array_agg(g.name ORDER BY g.name) FROM group_members m
                                JOIN datahub_groups g ON g.id = m.group_id WHERE m.user_id = u.id), '{}') AS groups,
                      coalesce((SELECT array_agg(g.name ORDER BY g.name) FROM group_members m
                                JOIN datahub_groups g ON g.id = m.group_id
                                WHERE m.user_id = u.id AND m.is_manager), '{}') AS managed_groups
               FROM datahub_users u WHERE u.id = %s
                 AND (%s::uuid IS NULL OR EXISTS (SELECT 1 FROM user_sessions s WHERE s.id = %s::uuid
                      AND s.user_id = u.id AND s.is_active AND s.logged_out_at IS NULL
                      AND s.logged_in_at > NOW() - INTERVAL '8 hours'))""",
            (session_id, user_id, session_id, session_id),
        )
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _get_user_by_username(username: str) -> Optional[dict]:
    try:
        conn = _get_db_conn()
        cur  = conn.cursor()
        cur.execute(
            "SELECT id, username, email, password_hash, role, is_active, must_change_password "
            "FROM datahub_users WHERE username = %s",
            (username,),
        )
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def get_current_user(request: Request) -> Optional[dict]:
    """Return the logged-in user dict, or None if not authenticated.
    The signed cookie carries the user id and a server-side session id; both must be valid."""
    uid, sid = request.session.get("user_id"), request.session.get("session_db_id")
    if not uid or not sid:
        request.session.clear()
        return None
    try:
        uuid_module.UUID(str(sid))
    except ValueError:
        request.session.clear()
        return None
    user = _get_user_by_id(uid, sid)
    if not user or not user["is_active"]:
        request.session.clear()
        return None
    return user


_PASSWORD_CHANGE_PATHS = frozenset({"/profile", "/profile/password", "/api/password", "/logout"})


def require_user(request: Request) -> dict:
    """Dependency: any authenticated user (viewer / uploader / admin).
    Accounts with a temporary password can only change it until they have done so."""
    user = get_current_user(request)
    if user is None:
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=401, detail="Not authenticated")
        raise HTTPException(status_code=307, headers={"Location": f"/login?next={request.url.path}"})
    if user.get("must_change_password") and request.url.path not in _PASSWORD_CHANGE_PATHS:
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=403, detail="Password change required (POST /api/password)")
        raise HTTPException(status_code=303, headers={"Location": "/profile?force=1"})
    return user


MIN_PASSWORD_LEN = max(8, int(os.environ.get("DATAHUB_MIN_PASSWORD_LENGTH", "12") or 12))
_DUMMY_HASH = None


def _check_password(user: Optional[dict], password: str) -> bool:
    """Verify a password; for unknown users verify against a dummy hash so that the
    response time does not reveal whether a user name exists."""
    global _DUMMY_HASH
    if not user:
        _DUMMY_HASH = _DUMMY_HASH or hash_password("dummy-password-for-timing")
        verify_password(password, _DUMMY_HASH)
        return False
    return bool(user["is_active"]) and verify_password(password, user["password_hash"])


def _safe_next(target: str) -> str:
    """Only local paths: '/x' is allowed, '//host', '/\\host' and absolute URLs are not."""
    if not target or not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return "/"
    return target


def _open_session(request: Request, user: dict, client_ip: Optional[str]) -> str:
    """Start a fresh session: new cookie content and a server-side session row."""
    sid = str(uuid_module.uuid4())
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute("UPDATE datahub_users SET last_login = NOW() WHERE id = %s", (str(user["id"]),))
    cur.execute("""INSERT INTO user_sessions (id, user_id, username, ip_address, user_agent)
                   VALUES (%s, %s, %s, %s, %s)""",
                (sid, str(user["id"]), user["username"], client_ip, (request.headers.get("user-agent") or "")[:512]))
    conn.commit(); conn.close()
    request.session.clear()
    request.session["user_id"] = str(user["id"])
    request.session["session_db_id"] = sid
    return sid


def _end_sessions(user_id: str, keep: Optional[str] = None) -> None:
    """Close all open sessions of a user (except `keep`)."""
    conn = _get_db_conn()
    conn.cursor().execute("""UPDATE user_sessions SET is_active = FALSE, logged_out_at = coalesce(logged_out_at, NOW())
                             WHERE user_id = %s AND is_active AND (%s::uuid IS NULL OR id <> %s::uuid)""",
                          (str(user_id), keep, keep))
    conn.commit(); conn.close()


def require_uploader(request: Request) -> dict:
    """Dependency: uploader or admin role required."""
    user = require_user(request)
    if user["role"] not in ("uploader", "admin"):
        raise HTTPException(status_code=403, detail="Uploader or admin role required.")
    return user


def require_admin(request: Request) -> dict:
    """Dependency: admin role required."""
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin role required.")
    return user


def _user_can_access(user: dict, allowed_orgs: list, allowed_groups: Optional[list] = None,
                     owner: Optional[str] = None) -> bool:
    """May the user see a dataset? Unrestricted (no organisations and no groups listed) means
    every authenticated user; otherwise admins, the uploader, members of a listed organisation
    and members of a listed project group."""
    allowed_groups = allowed_groups or []
    if not allowed_orgs and not allowed_groups:
        return True
    if user.get("role") == "admin" or (owner and user.get("username") == owner):
        return True
    if user.get("organization") and user.get("organization") in allowed_orgs:
        return True
    return bool(set(user.get("groups") or []) & set(allowed_groups))


def _parse_kv_list(raw: str) -> dict:
    """Parse 'a=b, c=d' into {'a': 'b', 'c': 'd'}."""
    out = {}
    for item in (raw or "").split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip().lower()] = v.strip()
    return out


# Organisations are configured per deployment, never hard-coded.
#   DATAHUB_ORGANIZATIONS="OrgA,OrgB,OrgC"
#   DATAHUB_ORG_EMAIL_DOMAINS="orga.example=OrgA,orgb.example=OrgB"
#   DATAHUB_ORG_HIDDEN_FILE_CLASSES="OrgB=input|mesh"
ORGANIZATIONS = [o.strip() for o in os.environ.get("DATAHUB_ORGANIZATIONS", "").split(",") if o.strip()]
_EMAIL_DOMAIN_TO_ORG = _parse_kv_list(os.environ.get("DATAHUB_ORG_EMAIL_DOMAINS", ""))
_ORG_HIDDEN_TYPES = {
    org: set(t.strip() for t in types.split("|") if t.strip())
    for org, types in (
        (k, v) for k, v in (
            (item.split("=", 1) + [""])[:2]
            for item in os.environ.get("DATAHUB_ORG_HIDDEN_FILE_CLASSES", "").split(",")
            if "=" in item
        )
    )
}
_ORG_HIDDEN_TYPES = {k.strip(): v for k, v in _ORG_HIDDEN_TYPES.items() if k.strip()}


def _valid_org(org: Optional[str]) -> bool:
    return org is None or org in ORGANIZATIONS


def _hides_file(user: dict, file_class: Optional[str]) -> bool:
    """True if the user's organisation is configured not to see this file class."""
    if user.get("role") == "admin":
        return False
    hidden = _ORG_HIDDEN_TYPES.get(user.get("organization") or "", set())
    return file_class in hidden


def _user_can_delete(user: dict, owner_username: Optional[str]) -> bool:
    """Only an admin or the original uploader may delete a dataset."""
    return user.get("role") == "admin" or (owner_username is not None and user.get("username") == owner_username)


def _org_from_email(email: str) -> Optional[str]:
    """Infer organisation from the e-mail domain (DATAHUB_ORG_EMAIL_DOMAINS)."""
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].lower()
    return _EMAIL_DOMAIN_TO_ORG.get(domain)


# ── General helpers ───────────────────────────────────────────────

def _get_minio() -> Minio:
    return Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS, secret_key=MINIO_SECRET, secure=MINIO_SECURE)


def _get_db_conn():
    return psycopg2.connect(**_DB_CONNECT, cursor_factory=psycopg2.extras.RealDictCursor)


def _human_size(n: Optional[int]) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_value(v) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    return "-" if v is None else str(v)


# ── Template globals ──────────────────────────────────────────────

templates.env.globals["human_size"] = _human_size
templates.env.globals["fmt_value"] = _fmt_value
templates.env.globals["fmt_short"] = lambda v: f"{v:.4g}" if isinstance(v, float) else _fmt_value(v)
templates.env.globals["ORGANIZATIONS"] = ORGANIZATIONS
templates.env.globals["ORG_EMAIL_DOMAINS"] = _EMAIL_DOMAIN_TO_ORG
templates.env.globals["SCHEMA"] = SCHEMA
templates.env.globals["MIN_PASSWORD_LEN"] = MIN_PASSWORD_LEN
templates.env.globals["SITE_NAME"] = os.environ.get("DATAHUB_SITE_NAME") or SCHEMA.instance_name
templates.env.globals["SITE_TAGLINE"] = os.environ.get("DATAHUB_SITE_TAGLINE", SCHEMA.description or "Data catalogue")


# ── Auth routes ───────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: str = ""):
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "next": next, "error": error,
    })


@app.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    client_ip = request.client.host if request.client else "unknown"
    rate_key_ip   = f"ip:{client_ip}"
    rate_key_user = f"user:{username.lower()}"

    # Check rate limit before even touching the DB
    if _check_login_rate(rate_key_ip) or _check_login_rate(rate_key_user):
        return templates.TemplateResponse(request, "login.html", {
            "next": next,
            "error": "Too many failed attempts. Please wait a few minutes before trying again.",
        }, status_code=429)

    user = _get_user_by_username(username)
    if not _check_password(user, password):
        _record_login_failure(rate_key_ip)
        _record_login_failure(rate_key_user)
        return templates.TemplateResponse(request, "login.html", {
            "next": next,
            "error": "Invalid username or password.",
        }, status_code=401)

    # Successful login: clear failure counters, rotate the session
    _clear_login_failures(rate_key_ip)
    _clear_login_failures(rate_key_user)
    _open_session(request, user, client_ip)
    safe_next = _safe_next(next)

    # If the user must change their password on first login, force them to do so
    # before continuing to any other page.
    if user.get("must_change_password"):
        return RedirectResponse(url="/profile?force=1", status_code=303)

    return RedirectResponse(url=safe_next, status_code=303)


@app.get("/logout")
async def logout(request: Request):
    # Close the DB session-tracking row before clearing the cookie
    session_db_id = request.session.get("session_db_id")
    if session_db_id:
        try:
            conn = _get_db_conn()
            conn.cursor().execute(
                "UPDATE user_sessions SET logged_out_at = NOW(), is_active = FALSE WHERE id = %s",
                (session_db_id,),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ── Profile: change own password ──────────────────────────────────

@app.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    msg: str = "",
    error: str = "",
    force: int = 0,
    user: dict = Depends(require_user),
):
    """Profile page — available to every authenticated user (viewer/uploader/admin)."""
    # If must_change_password is set in session user but not in force param, add it
    full_user = _get_user_by_username(user["username"])
    must_change = bool(full_user and full_user.get("must_change_password")) if full_user else False
    return templates.TemplateResponse(request, "profile.html", {
        "current_user": user,
        "msg":   msg,
        "error": error,
        "force": force or must_change,
    })


@app.post("/profile/password")
async def change_own_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    force: int = Form(0),
    user: dict = Depends(require_user),
):
    if new_password != confirm_password:
        return RedirectResponse(url=f"/profile?force={force}&error=New+passwords+do+not+match", status_code=303)
    if len(new_password) < MIN_PASSWORD_LEN:
        return RedirectResponse(url=f"/profile?force={force}&error=Password+must+be+at+least+{MIN_PASSWORD_LEN}+characters", status_code=303)
    full_user = _get_user_by_username(user["username"])
    if not full_user or not verify_password(current_password, full_user["password_hash"]):
        return RedirectResponse(url=f"/profile?force={force}&error=Current+password+is+incorrect", status_code=303)
    try:
        conn = _get_db_conn()
        conn.cursor().execute(
            "UPDATE datahub_users SET password_hash = %s, must_change_password = FALSE WHERE id = %s",
            (hash_password(new_password), str(user["id"])),
        )
        conn.commit(); conn.close()
    except Exception:
        logger.exception("password change failed")
        return RedirectResponse(url=f"/profile?force={force}&error=Password+could+not+be+changed", status_code=303)
    _end_sessions(str(user["id"]), keep=user.get("session_id"))
    return RedirectResponse(url="/profile?msg=Password+changed+successfully", status_code=303)


# ── Admin: redirect /admin → /admin/stats ────────────────────────────────────

@app.get("/admin")
async def admin_redirect():
    return RedirectResponse(url="/admin/stats", status_code=302)


# ── Admin: user management ────────────────────────────────────────

@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    admin: dict = Depends(require_admin),
    msg: str = "",
    error: str = "",
):
    conn = _get_db_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT id, username, email, role, is_active, created_at, last_login, created_by "
        "FROM datahub_users ORDER BY created_at ASC"
    )
    users = [dict(r) for r in cur.fetchall()]
    conn.close()
    return templates.TemplateResponse(request, "admin_users.html", {
        "current_user": admin,
        "users": users,
        "msg": msg,
        "error": error,
    })


@app.post("/admin/users")
async def admin_create_user(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    password: str = Form(...),
    role: str = Form("viewer"),
    organization: str = Form(""),
    admin: dict = Depends(require_admin),
):
    if len(password) < MIN_PASSWORD_LEN:
        return RedirectResponse(url="/admin/users?error=Password+must+be+at+least+8+chars",
                                status_code=303)
    if role not in ("admin", "uploader", "viewer"):
        return RedirectResponse(url="/admin/users?error=Invalid+role", status_code=303)
    org_val = organization.strip() if organization.strip() else None
    if not org_val and email:
        org_val = _org_from_email(email)
    if org_val and not _valid_org(org_val):
        return RedirectResponse(url="/admin/users?error=Invalid+organization", status_code=303)
    try:
        conn = _get_db_conn()
        conn.cursor().execute(
            """INSERT INTO datahub_users (username, email, password_hash, role, created_by, must_change_password, organization)
               VALUES (%s, %s, %s, %s, %s, TRUE, %s)""",
            (username, email or None, hash_password(password), role, admin["username"], org_val),
        )
        conn.commit(); conn.close()
    except psycopg2.errors.UniqueViolation:
        return RedirectResponse(
            url=f"/admin/users?error=Username+%27{username}%27+already+exists", status_code=303
        )
    except Exception as exc:
        return RedirectResponse(url=f"/admin/users?error={exc}", status_code=303)
    return RedirectResponse(url=f"/admin/stats#users", status_code=303)


@app.post("/admin/users/{user_id}/role")
async def admin_change_role(
    user_id: str,
    request: Request,
    role: str = Form(...),
    admin: dict = Depends(require_admin),
):
    if role not in ("admin", "uploader", "viewer"):
        raise HTTPException(status_code=400, detail="Invalid role")
    conn = _get_db_conn()
    conn.cursor().execute(
        "UPDATE datahub_users SET role = %s WHERE id = %s", (role, user_id)
    )
    conn.commit(); conn.close()
    return RedirectResponse(url="/admin/stats#users", status_code=303)


@app.post("/admin/users/{user_id}/org")
async def admin_set_org(
    user_id: str,
    request: Request,
    organization: str = Form(""),
    admin: dict = Depends(require_admin),
):
    org_val = organization.strip() if organization.strip() else None
    if org_val and not _valid_org(org_val):
        raise HTTPException(status_code=400, detail="Invalid organization")
    conn = _get_db_conn()
    conn.cursor().execute(
        "UPDATE datahub_users SET organization = %s WHERE id = %s", (org_val, user_id)
    )
    conn.commit(); conn.close()
    return RedirectResponse(url="/admin/stats#users", status_code=303)


@app.post("/admin/users/{user_id}/toggle")
async def admin_toggle_user(
    user_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
):
    if str(user_id) == str(admin["id"]):
        return RedirectResponse(url="/admin/users?error=Cannot+deactivate+yourself", status_code=303)
    conn = _get_db_conn()
    cur  = conn.cursor()
    cur.execute("UPDATE datahub_users SET is_active = NOT is_active WHERE id = %s RETURNING is_active",
                (user_id,))
    row    = cur.fetchone()
    conn.commit(); conn.close()
    state  = "activated" if row and row["is_active"] else "deactivated"
    if state == "deactivated":
        _end_sessions(user_id)
    return RedirectResponse(url=f"/admin/stats#users", status_code=303)


@app.post("/admin/users/{user_id}/password")
async def admin_reset_password(
    user_id: str,
    request: Request,
    new_password: str = Form(...),
    admin: dict = Depends(require_admin),
):
    if len(new_password) < MIN_PASSWORD_LEN:
        return RedirectResponse(url=f"/admin/users?error=Password+must+be+at+least+{MIN_PASSWORD_LEN}+chars",
                                status_code=303)
    conn = _get_db_conn()
    conn.cursor().execute(
        "UPDATE datahub_users SET password_hash = %s, must_change_password = TRUE WHERE id = %s",
        (hash_password(new_password), user_id),
    )
    conn.commit(); conn.close()
    _end_sessions(user_id)
    return RedirectResponse(url="/admin/stats#users?msg=Password+reset", status_code=303)


@app.delete("/admin/users/{user_id}")
async def admin_delete_user(
    user_id: str,
    request: Request,
    admin: dict = Depends(require_admin),
):
    if str(user_id) == str(admin["id"]):
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    conn = _get_db_conn()
    conn.cursor().execute("DELETE FROM datahub_users WHERE id = %s", (user_id,))
    conn.commit(); conn.close()
    return {"deleted": True}


# ── Admin: activity stats ─────────────────────────────────────────

@app.get("/admin/stats", response_class=HTMLResponse)
async def admin_stats_page(
    request: Request,
    admin: dict = Depends(require_admin),
):
    """Admin dashboard: active sessions, download statistics, recent activity."""
    conn = _get_db_conn()
    cur  = conn.cursor()

    # ── Who is online now?
    # Primary signal: last_seen_at heartbeat (updated on every page load).
    # We use a 15-minute window so that someone who closed the tab without
    # logging out is considered "gone" after 15 minutes of inactivity.
    # Fallback: show anyone whose login session is still open in user_sessions
    # even if the heartbeat hasn't fired yet (e.g. they just logged in).
    cur.execute("""
        SELECT
            u.id,
            u.username,
            u.role,
            u.last_seen_at,
            u.last_login,
            EXTRACT(EPOCH FROM (NOW() - COALESCE(u.last_seen_at, u.last_login))) / 60
                AS minutes_ago,
            s.ip_address,
            s.user_agent,
            s.logged_in_at
        FROM datahub_users u
        LEFT JOIN LATERAL (
            SELECT ip_address, user_agent, logged_in_at
            FROM user_sessions
            WHERE user_id = u.id
              AND is_active = TRUE
            ORDER BY logged_in_at DESC
            LIMIT 1
        ) s ON TRUE
        WHERE u.is_active = TRUE
          AND (
            -- seen in the last 15 minutes via heartbeat
            u.last_seen_at > NOW() - INTERVAL '15 minutes'
            OR
            -- or has an open session row started within the last 8 hours
            s.logged_in_at IS NOT NULL
          )
        ORDER BY COALESCE(u.last_seen_at, u.last_login) DESC NULLS LAST
    """)
    active_sessions = [dict(r) for r in cur.fetchall()]

    # ── Per-user download summary ────────────────────────────────────────────
    cur.execute("""
        SELECT username,
               COUNT(*)                                         AS total_downloads,
               COALESCE(SUM(size_bytes), 0)                    AS total_bytes,
               MAX(downloaded_at)                              AS last_download,
               COUNT(DISTINCT DATE(downloaded_at))             AS active_days
        FROM download_logs
        GROUP BY username
        ORDER BY total_bytes DESC
    """)
    user_dl_stats = [dict(r) for r in cur.fetchall()]

    # ── Recent downloads (last 100) ─────────────────────────────────────────
    cur.execute("""
        SELECT username, filename, object_key, size_bytes, downloaded_at
        FROM download_logs
        ORDER BY downloaded_at DESC
        LIMIT 100
    """)
    recent_downloads = [dict(r) for r in cur.fetchall()]

    # ── Aggregate headline numbers ───────────────────────────────────────────
    cur.execute("SELECT COUNT(*) AS n FROM datahub_users WHERE is_active")
    total_users = cur.fetchone()["n"]

    cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS b FROM download_logs")
    dl_row = cur.fetchone()
    total_dl_count = dl_row["n"]
    total_dl_bytes = dl_row["b"]

    cur.execute("""
        SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS b
        FROM download_logs
        WHERE downloaded_at > NOW() - INTERVAL '24 hours'
    """)
    today_row = cur.fetchone()
    today_dl_count = today_row["n"]
    today_dl_bytes = today_row["b"]

    # ── Downloads per day (last 14 days) for the sparkline chart ────────────
    cur.execute("""
        SELECT DATE(downloaded_at) AS day,
               COUNT(*)            AS cnt,
               COALESCE(SUM(size_bytes), 0) AS bytes
        FROM download_logs
        WHERE downloaded_at > NOW() - INTERVAL '14 days'
        GROUP BY day
        ORDER BY day
    """)
    dl_by_day = [dict(r) for r in cur.fetchall()]

    # ── Top downloaded files ─────────────────────────────────────────────────
    cur.execute("""
        SELECT filename,
               COUNT(*)                       AS downloads,
               COALESCE(SUM(size_bytes), 0)   AS total_bytes
        FROM download_logs
        WHERE filename IS NOT NULL AND filename != ''
        GROUP BY filename
        ORDER BY downloads DESC
        LIMIT 20
    """)
    top_files = [dict(r) for r in cur.fetchall()]

    conn.close()

    # ── Full user list (for the Users tab) ──────────────────────────────────
    conn2 = _get_db_conn()
    cur2  = conn2.cursor()
    cur2.execute("""
        SELECT id, username, email, role, is_active, last_login, created_at, must_change_password, organization
        FROM datahub_users
        ORDER BY created_at
    """)
    users = [dict(r) for r in cur2.fetchall()]
    conn2.close()

    def _fmt_bytes(b: int) -> str:
        b = int(b or 0)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} PB"

    return templates.TemplateResponse(request, "admin_stats.html", {
        "current_user":    admin,
        "users":           users,
        "active_sessions": active_sessions,
        "user_dl_stats":   user_dl_stats,
        "recent_downloads":recent_downloads,
        "total_users":     total_users,
        "total_dl_count":  total_dl_count,
        "total_dl_bytes":  _fmt_bytes(total_dl_bytes),
        "today_dl_count":  today_dl_count,
        "today_dl_bytes":  _fmt_bytes(today_dl_bytes),
        "dl_by_day":       dl_by_day,
        "top_files":       top_files,
        "fmt_bytes":       _fmt_bytes,
        "tz_name":         _TZ,
    })


# ── API: mark stale sessions as expired (called by cron / health check) ───────

@app.post("/api/admin/expire-sessions")
async def expire_stale_sessions(admin: dict = Depends(require_admin)):
    """Close user_sessions rows that are older than the 8-hour cookie lifetime."""
    conn = _get_db_conn()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE user_sessions
        SET is_active = FALSE, logged_out_at = NOW()
        WHERE is_active = TRUE
          AND logged_in_at < NOW() - INTERVAL '8 hours'
    """)
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return {"expired": affected}



# ── JSON authentication for scripts and the SDK ───────────────────

@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    username, password = body.get("username", ""), body.get("password", "")
    client_ip = request.client.host if request.client else "unknown"
    if _check_login_rate(f"ip:{client_ip}") or _check_login_rate(f"user:{username.lower()}"):
        raise HTTPException(status_code=429, detail="Too many failed attempts; try again later.")
    user = _get_user_by_username(username)
    if not _check_password(user, password):
        _record_login_failure(f"ip:{client_ip}"); _record_login_failure(f"user:{username.lower()}")
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _clear_login_failures(f"ip:{client_ip}"); _clear_login_failures(f"user:{username.lower()}")
    _open_session(request, user, client_ip)
    return {"username": user["username"], "role": user["role"], "must_change_password": user["must_change_password"]}


@app.post("/api/password")
async def api_change_password(request: Request, user: dict = Depends(require_user)):
    body = await request.json()
    full = _get_user_by_username(user["username"])
    if not verify_password(body.get("current_password", ""), full["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    new = body.get("new_password", "")
    if len(new) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LEN} characters")
    conn = _get_db_conn()
    conn.cursor().execute("UPDATE datahub_users SET password_hash=%s, must_change_password=FALSE WHERE id=%s",
                          (hash_password(new), str(user["id"])))
    conn.commit(); conn.close()
    _end_sessions(str(user["id"]), keep=user.get("session_id"))
    return {"changed": True}


# ── Access helpers for catalogue queries ──────────────────────────

def _access_sql(user: dict, alias: str = "d") -> tuple[str, list]:
    """SQL fragment restricting datasets to those the user may see."""
    if user.get("role") == "admin":
        return "TRUE", []
    return (f"(({alias}.allowed_orgs = '{{}}' AND {alias}.allowed_groups = '{{}}') OR %s = ANY({alias}.allowed_orgs)"
            f" OR {alias}.allowed_groups && %s::text[] OR {alias}.uploaded_by = %s)",
            [user.get("organization") or "", list(user.get("groups") or []), user.get("username")])


def _get_dataset(ds_id: str, user: dict) -> dict:
    try:
        uuid_module.UUID(ds_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Dataset not found")
    conn = _get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM datasets WHERE id = %s", (ds_id,))
    ds = cur.fetchone()
    if not ds:
        conn.close()
        raise HTTPException(status_code=404, detail="Dataset not found")
    if not _user_can_access(user, ds["allowed_orgs"] or [], ds["allowed_groups"] or [], ds["uploaded_by"]):
        conn.close()
        raise HTTPException(status_code=403, detail="Access restricted to specific organisations or groups")
    ds = dict(ds); ds.pop("fts_vector", None)
    cur.execute("SELECT id, part_index, label, metadata FROM dataset_parts WHERE dataset_id=%s ORDER BY part_index, label", (ds_id,))
    parts = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT id, part_id, file_class, relpath, filename, size_bytes, sha256, time_value
                   FROM files WHERE dataset_id=%s ORDER BY relpath""", (ds_id,))
    files = [dict(r) for r in cur.fetchall() if not _hides_file(user, r["file_class"])]
    cur.execute("SELECT part_id, name, value, unit, source FROM quantities WHERE dataset_id=%s ORDER BY name", (ds_id,))
    qs = [dict(r) for r in cur.fetchall()]
    conn.close()
    for p in parts:
        p["files"] = [f for f in files if f["part_id"] == p["id"]]
        p["quantities"] = [q for q in qs if q["part_id"] == p["id"]]
    ds["parts"] = parts
    ds["files"] = [f for f in files if f["part_id"] is None]
    ds["quantities"] = [q for q in qs if q["part_id"] is None]
    ds["visible_files"] = len(files)
    return ds


_OPS = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}


def _search(user: dict, q: str = "", dataset_type: str = "", status: str = "", tag: str = "",
            param: str = "", op: str = "", value: str = "", quantity: str = "", qop: str = "", qvalue: str = "",
            limit: int = 25, offset: int = 0) -> tuple[list[dict], int]:
    where, args = [], []
    acc, acc_args = _access_sql(user)
    where.append(acc); args += acc_args
    if q:
        where.append("(d.fts_vector @@ plainto_tsquery('simple', %s) OR d.name ILIKE %s)"); args += [q, f"%{q}%"]
    if dataset_type:
        where.append("d.dataset_type = %s"); args.append(dataset_type)
    if status:
        where.append("d.status = %s"); args.append(status)
    if tag:
        where.append("%s = ANY(d.tags)"); args.append(tag)
    if param and op in _OPS and value != "":
        p = SCHEMA.parameter(param)
        if p and p.type in ("float", "int"):
            where.append(f"(d.parameters->>%s) ~ '^[-+0-9.eE]+$' AND (d.parameters->>%s)::double precision {_OPS[op]} %s")
            args += [param, param, float(value)]
        else:
            where.append(f"(d.parameters->>%s) {_OPS[op]} %s"); args += [param, value]
    if quantity and qop in _OPS and qvalue != "":
        # nosec B608: constant SQL fragments; all values are bound parameters
        where.append(f"EXISTS (SELECT 1 FROM quantities qq WHERE qq.dataset_id = d.id AND qq.part_id IS NULL "  # nosec B608
                     f"AND qq.name = %s AND qq.value {_OPS[qop]} %s)")  # nosec B608
        args += [quantity, float(qvalue)]
    sql_where = " AND ".join(where)
    conn = _get_db_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT count(*) AS n FROM datasets d WHERE {sql_where}", args)  # nosec B608: constant SQL fragments; all values are bound parameters
    total = cur.fetchone()["n"]
    select = """SELECT d.id, d.name, d.dataset_type, d.status, d.tags, d.allowed_orgs, d.allowed_groups, d.uploaded_by,
                       d.created_at, d.started_at, d.finished_at, d.file_count, d.total_bytes, d.parameters,
                       (SELECT count(*) FROM dataset_parts p WHERE p.dataset_id = d.id) AS part_count
                FROM datasets d WHERE """
    cur.execute(select + sql_where + " ORDER BY d.created_at DESC, d.name LIMIT %s OFFSET %s", args + [limit, offset])  # nosec B608
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows, total


# ── Pages: datasets ───────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def search_page(request: Request, user: dict = Depends(require_user), q: str = "", type: str = "",
                      status: str = "", tag: str = "", param: str = "", op: str = "ge", value: str = "",
                      quantity: str = "", qop: str = "ge", qvalue: str = "", page: int = 1, per_page: int = 25):
    per_page = max(5, min(per_page, 200))
    page = max(page, 1)
    try:
        rows, total = _search(user, q, type, status, tag, param, op, value, quantity, qop, qvalue,
                              per_page, (page - 1) * per_page)
        error = ""
    except (ValueError, psycopg2.Error) as exc:
        rows, total, error = [], 0, f"Invalid filter: {exc}"
    conn = _get_db_conn(); cur = conn.cursor()
    acc, acc_args = _access_sql(user)
    cur.execute(f"SELECT dataset_type, count(*) AS n FROM datasets d WHERE {acc} GROUP BY dataset_type", acc_args)  # nosec B608: constant SQL fragments; all values are bound parameters
    by_type = {r["dataset_type"]: r["n"] for r in cur.fetchall()}
    cur.execute(f"SELECT coalesce(sum(total_bytes),0) AS b, coalesce(sum(file_count),0) AS f FROM datasets d WHERE {acc}", acc_args)  # nosec B608: constant SQL fragments; all values are bound parameters
    sizes = cur.fetchone()
    conn.close()
    return templates.TemplateResponse(request, "search.html", {
        "current_user": user, "rows": rows, "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page), "by_type": by_type, "sizes": sizes, "error": error,
        "f": {"q": q, "type": type, "status": status, "tag": tag, "param": param, "op": op, "value": value,
              "quantity": quantity, "qop": qop, "qvalue": qvalue},
    })


@app.get("/dataset/{ds_id}", response_class=HTMLResponse)
async def dataset_page(request: Request, ds_id: str, user: dict = Depends(require_user), share_error: str = "",
                       error: str = ""):
    ds = _get_dataset(ds_id, user)
    owner = _user_can_delete(user, ds.get("uploaded_by"))
    return templates.TemplateResponse(request, "dataset.html", {
        "current_user": user, "ds": ds, "can_delete": owner, "can_share": owner and SHARE_MAX_DAYS > 0,
        "shares": _list_shares(user, ds_id) if owner else [], "share_max_days": SHARE_MAX_DAYS,
        "new_share_url": request.session.pop("new_share_url", None), "share_error": share_error, "error": error,
        "group_choices": _group_choices(user, ds.get("allowed_groups") or []),
        "dtype": next((t for t in SCHEMA.dataset_types if t.id == ds["dataset_type"]), None),
        "class_labels": {c.id: c.label for c in SCHEMA.file_classes} | {"other": "Other"},
        "qlabels": {q.name: q for q in SCHEMA.quantities}, "plabels": {p.name: p for p in SCHEMA.parameters},
    })


@app.get("/schema", response_class=HTMLResponse)
async def schema_page(request: Request, user: dict = Depends(require_user)):
    path = Path(os.environ.get("DATAHUB_SCHEMA", "config/schema.yaml"))
    return templates.TemplateResponse(request, "schema.html", {
        "current_user": user, "schema": SCHEMA, "schema_text": path.read_text() if path.exists() else ""})


def _group_choices(user: dict, keep: Optional[list] = None) -> list[str]:
    """Groups a user may assign to a dataset (all for admins), plus those already assigned."""
    if user.get("role") == "admin":
        conn = _get_db_conn(); cur = conn.cursor()
        cur.execute("SELECT name FROM datahub_groups ORDER BY name")
        names = [r["name"] for r in cur.fetchall()]; conn.close()
    else:
        names = list(user.get("groups") or [])
    return sorted(set(names) | set(keep or []))


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request, user: dict = Depends(require_uploader)):
    return templates.TemplateResponse(request, "upload.html", {"current_user": user, "group_choices": _group_choices(user)})


# ── JSON API: schema and datasets ─────────────────────────────────

@app.get("/api/schema")
async def api_schema(user: dict = Depends(require_user)):
    return SCHEMA.to_public_dict()


@app.get("/api/datasets")
async def api_datasets(user: dict = Depends(require_user), q: str = "", type: str = "", status: str = "",
                       tag: str = "", param: str = "", op: str = "", value: str = "", quantity: str = "",
                       qop: str = "", qvalue: str = "", limit: int = 50, offset: int = 0):
    try:
        rows, total = _search(user, q, type, status, tag, param, op, value, quantity, qop, qvalue,
                              max(1, min(limit, 1000)), max(0, offset))
    except (ValueError, psycopg2.Error) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid filter: {exc}")
    return JSONResponse(json.loads(json.dumps({"total": total, "items": rows}, default=str)))


@app.get("/api/datasets/{ds_id}")
async def api_dataset(ds_id: str, user: dict = Depends(require_user)):
    return JSONResponse(json.loads(json.dumps(_get_dataset(ds_id, user), default=str)))


@app.delete("/api/datasets/{ds_id}")
async def api_delete_dataset(ds_id: str, user: dict = Depends(require_user)):
    ds = _get_dataset(ds_id, user)
    if not _user_can_delete(user, ds.get("uploaded_by")):
        raise HTTPException(status_code=403, detail="Only an admin or the uploader can delete this dataset.")
    await run_in_threadpool(delete_dataset, ds_id)
    return {"deleted": True}


@app.get("/api/tags")
async def api_tags(user: dict = Depends(require_user)):
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT DISTINCT unnest(tags) AS t FROM datasets ORDER BY 1")
    tags = [r["t"] for r in cur.fetchall()]
    conn.close()
    return tags


# ── Upload (three phases: start, file ..., commit) ────────────────

_SAFE_NAME = re.compile(r"^[\w.\-=+@,]+$")


def _safe_relpath(rel: str) -> str:
    rel = rel.replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise HTTPException(status_code=400, detail=f"Invalid relative path: {rel}")
    return "/".join(parts)


@app.post("/api/upload/start")
async def upload_start(user: dict = Depends(require_uploader), name: str = Form(...), dataset_type: str = Form(...),
                       tags: str = Form(""), notes: str = Form(""), metadata: str = Form("{}"),
                       allowed_orgs: str = Form(""), allowed_groups: str = Form(""), status: str = Form("completed")):
    name = name.strip()
    if not _SAFE_NAME.match(name):
        raise HTTPException(status_code=400, detail="Name may contain letters, digits and . - _ = + @ , only")
    try:
        SCHEMA.dataset_type(dataset_type)
        meta = json.loads(metadata or "{}")
        assert isinstance(meta, dict)
    except (SchemaError, ValueError, AssertionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    orgs = [o.strip() for o in allowed_orgs.split(",") if o.strip()]
    if any(not _valid_org(o) for o in orgs):
        raise HTTPException(status_code=400, detail="Unknown organisation in allowed_orgs")
    groups = _check_groups(user, [g.strip() for g in allowed_groups.split(",") if g.strip()])
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT 1 FROM datasets WHERE name=%s", (name,))
    exists = cur.fetchone(); conn.close()
    if exists:
        raise HTTPException(status_code=409, detail=f"A dataset named '{name}' already exists")
    upload_id = uuid_module.uuid4().hex
    root = UPLOAD_DIR / upload_id / name
    root.mkdir(parents=True, exist_ok=True)
    # Session state lives on disk so that every worker process can serve the upload.
    (UPLOAD_DIR / upload_id / "session.json").write_text(json.dumps({
        "user": user["username"], "name": name, "type": dataset_type,
        "tags": [t.strip() for t in tags.split(",") if t.strip()], "notes": notes or None,
        "metadata": meta, "orgs": orgs, "groups": groups, "status": status}))
    return {"upload_id": upload_id}


def _session(upload_id: str, user: dict) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise HTTPException(status_code=404, detail="Upload session not found")
    meta = UPLOAD_DIR / upload_id / "session.json"
    try:
        s = json.loads(meta.read_text())
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Upload session not found")
    if s["user"] != user["username"]:
        raise HTTPException(status_code=404, detail="Upload session not found")
    s["root"] = UPLOAD_DIR / upload_id / s["name"]
    return s


MAX_UPLOAD_BYTES = int(float(os.environ.get("DATAHUB_MAX_UPLOAD_GB", "200") or 200) * (1 << 30))
MAX_UPLOAD_FILES = int(os.environ.get("DATAHUB_MAX_UPLOAD_FILES", "100000") or 100000)


@app.post("/api/upload/file")
async def upload_file(user: dict = Depends(require_uploader), upload_id: str = Form(...), relpath: str = Form(...),
                      file: UploadFile = File(...)):
    s = _session(upload_id, user)
    rel = _safe_relpath(relpath)
    dest = s["root"] / rel
    # Per-upload limits protect the staging disk (DATAHUB_MAX_UPLOAD_GB, DATAHUB_MAX_UPLOAD_FILES).
    stored = [p for p in s["root"].rglob("*") if p.is_file() and p != dest]
    if len(stored) >= MAX_UPLOAD_FILES:
        raise HTTPException(status_code=413, detail=f"Upload exceeds {MAX_UPLOAD_FILES} files")
    used = sum(p.stat().st_size for p in stored)
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1 << 20):
            written += len(chunk)
            if used + written > MAX_UPLOAD_BYTES:
                out.close(); dest.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"Upload exceeds {MAX_UPLOAD_BYTES / (1 << 30):g} GB")
            out.write(chunk)
    return {"stored": rel, "file_class": SCHEMA.classify(rel)}


@app.post("/api/upload/commit")
async def upload_commit(user: dict = Depends(require_uploader), upload_id: str = Form(...)):
    s = _session(upload_id, user)
    stored = [p for p in s["root"].rglob("*") if p.is_file()]
    n_files, n_bytes = len(stored), sum(p.stat().st_size for p in stored)
    try:
        ds_id, _ = await run_in_threadpool(
            ingest_folder, s["root"], SCHEMA, s["type"], name=s["name"], tags=s["tags"], notes=s["notes"],
            metadata=s["metadata"], allowed_orgs=s["orgs"], allowed_groups=s.get("groups", []),
            uploaded_by=user["username"], status=s["status"],
            skip_existing=False)
    except (IngestError, SchemaError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        shutil.rmtree(s["root"].parent, ignore_errors=True)
    return {"dataset_id": ds_id, "files": n_files, "bytes": n_bytes}


@app.post("/api/upload/abort")
async def upload_abort(user: dict = Depends(require_uploader), upload_id: str = Form(...)):
    s = _session(upload_id, user)
    shutil.rmtree(s["root"].parent, ignore_errors=True)
    return {"aborted": True}


@app.post("/api/upload/preview")
async def upload_preview(user: dict = Depends(require_uploader), relpaths: str = Form(...), dataset_type: str = Form(...)):
    """Classify a list of relative paths (JSON array) without uploading, for the upload page."""
    paths = json.loads(relpaths)
    out = []
    for r in paths[:5000]:
        part = SCHEMA.part_of(r, dataset_type) if dataset_type else None
        out.append({"relpath": r, "file_class": "skip" if SCHEMA.skip(r) else SCHEMA.classify(r),
                    "part": part[1] if part else None})
    return out


# ── Downloads ─────────────────────────────────────────────────────

def _log_download(user: dict, row: dict):
    try:
        conn = _get_db_conn()
        conn.cursor().execute("""INSERT INTO download_logs (user_id, username, dataset_id, file_id, object_key, filename, size_bytes)
                                 VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                              (str(user["id"]), user["username"], row.get("dataset_id"), row.get("id"),
                               row.get("object_key"), row.get("filename"), row.get("size_bytes")))
        conn.commit(); conn.close()
    except Exception as exc:
        logger.warning("download log failed: %s", exc)


@app.get("/api/download/{file_id}")
async def api_download(file_id: str, user: dict = Depends(require_user)):
    try:
        uuid_module.UUID(file_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="File not found")
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute("""SELECT f.*, d.allowed_orgs, d.allowed_groups, d.uploaded_by
                   FROM files f JOIN datasets d ON d.id = f.dataset_id WHERE f.id = %s""", (file_id,))
    row = cur.fetchone(); conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="File not found")
    if (not _user_can_access(user, row["allowed_orgs"] or [], row["allowed_groups"] or [], row["uploaded_by"])
            or _hides_file(user, row["file_class"])):
        raise HTTPException(status_code=403, detail="Access restricted")
    _log_download(user, dict(row))
    obj = _get_minio().get_object(MINIO_BUCKET, row["object_key"])

    def stream():
        try:
            for chunk in obj.stream(1 << 16):
                yield chunk
        finally:
            obj.close(); obj.release_conn()
    ctype = mimetypes.guess_type(row["filename"])[0] or "application/octet-stream"
    return StreamingResponse(stream(), media_type=ctype, headers={
        "Content-Disposition": _content_disposition(row["filename"]), "Content-Length": str(row["size_bytes"] or "")})


@app.get("/api/datasets/{ds_id}/archive.zip")
async def api_download_archive(ds_id: str, user: dict = Depends(require_user)):
    """All files of a dataset the user may see, as a ZIP archive (with a metadata.json)."""
    ds = _get_dataset(ds_id, user)
    files = ds["files"] + [f for p in ds["parts"] for f in p["files"]]

    def build() -> str:
        mc = _get_minio()
        conn = _get_db_conn(); cur = conn.cursor()
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            meta = {k: v for k, v in ds.items() if k not in ("parts", "files")}
            meta["schema"] = SCHEMA.instance_name
            zf.writestr(f"{ds['name']}/datahub_metadata.json", json.dumps(meta, indent=2, default=str))
            for f in files:
                cur.execute("SELECT object_key FROM files WHERE id=%s", (f["id"],))
                key = cur.fetchone()["object_key"]
                obj = mc.get_object(MINIO_BUCKET, key)
                with zf.open(f"{ds['name']}/{f['relpath']}", "w") as out:
                    for chunk in obj.stream(1 << 16):
                        out.write(chunk)
                obj.close(); obj.release_conn()
        conn.close()
        return tmp.name
    path = await run_in_threadpool(build)
    _log_download(user, {"dataset_id": ds_id, "filename": f"{ds['name']}.zip", "size_bytes": os.path.getsize(path)})

    def stream():
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(1 << 16):
                    yield chunk
        finally:
            os.unlink(path)
    return StreamingResponse(stream(), media_type="application/zip",
                             headers={"Content-Disposition": _content_disposition(f"{ds['name']}.zip")})


# ── ml-api proxies (results filtered by access) ───────────────────

ML_API_TOKEN = os.environ.get("ML_API_TOKEN", "")


def _ml(method: str, path: str, **kw):
    headers = kw.pop("headers", {}) or {}
    if ML_API_TOKEN:
        headers["Authorization"] = f"Bearer {ML_API_TOKEN}"
    try:
        r = _requests.request(method, f"{ML_API_URL}{path}", timeout=kw.pop("timeout", 120), headers=headers, **kw)
    except _requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"ml-api unavailable: {exc}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.json().get("detail", r.text) if r.headers.get(
            "content-type", "").startswith("application/json") else r.text)
    return r.json()


def _check_groups(user: dict, groups: list[str]) -> list[str]:
    """Validate group names for a dataset: they must exist, and non-admins may only use their own groups."""
    groups = sorted(set(groups))
    if not groups:
        return []
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT name FROM datahub_groups WHERE name = ANY(%s)", (groups,))
    known = {r["name"] for r in cur.fetchall()}
    conn.close()
    unknown = [g for g in groups if g not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown group(s): {', '.join(unknown)}")
    if user.get("role") != "admin":
        foreign = [g for g in groups if g not in (user.get("groups") or [])]
        if foreign:
            raise HTTPException(status_code=403, detail=f"You are not a member of: {', '.join(foreign)}")
    return groups


def _visible_ids(user: dict, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    acc, args = _access_sql(user)
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute(f"SELECT id FROM datasets d WHERE d.id = ANY(%s::uuid[]) AND {acc}", [ids] + args)  # nosec B608: constant SQL fragments; all values are bound parameters
    out = {str(r["id"]) for r in cur.fetchall()}
    conn.close()
    return out


@app.get("/api/similar/params/{ds_id}")
async def api_similar_params(ds_id: str, n: int = 10, user: dict = Depends(require_user)):
    _get_dataset(ds_id, user)
    rows = _ml("GET", f"/similar/params/{ds_id}", params={"n": n * 3})
    ok = _visible_ids(user, [r["dataset_id"] for r in rows])
    return [r for r in rows if r["dataset_id"] in ok][:n]


@app.get("/api/similar/fields/{ds_id}")
async def api_similar_fields(ds_id: str, field: str = "", n: int = 10, user: dict = Depends(require_user)):
    _get_dataset(ds_id, user)
    rows = _ml("GET", f"/similar/fields/{ds_id}", params={"field": field, "n": n * 3})
    ok = _visible_ids(user, [r["dataset_id"] for r in rows])
    return [r for r in rows if r["dataset_id"] in ok][:n]


@app.post("/api/embed/{ds_id}")
async def api_embed(ds_id: str, user: dict = Depends(require_uploader)):
    _get_dataset(ds_id, user)
    return _ml("POST", f"/embed/{ds_id}", timeout=900)


@app.post("/api/process/{ds_id}")
async def api_process(ds_id: str, user: dict = Depends(require_uploader)):
    _get_dataset(ds_id, user)
    return _ml("POST", f"/process/{ds_id}")


@app.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request, user: dict = Depends(require_user)):
    return templates.TemplateResponse(request, "graph.html", {"current_user": user})


@app.get("/api/graph/overview")
async def api_graph_overview(limit: int = 500, user: dict = Depends(require_user)):
    g = _ml("GET", "/graph/overview", params={"limit": limit})
    ok = _visible_ids(user, [n["id"] for n in g["nodes"] if n.get("kind") == "dataset"])
    nodes = [n for n in g["nodes"] if n.get("kind") != "dataset" or n["id"] in ok]
    keep = {n["id"] for n in nodes}
    return {"nodes": nodes, "edges": [e for e in g["edges"] if e["source"] in keep and e["target"] in keep]}


@app.get("/api/graph/neighbors/{ds_id}")
async def api_graph_neighbors(ds_id: str, user: dict = Depends(require_user)):
    _get_dataset(ds_id, user)
    rows = _ml("GET", f"/graph/neighbors/{ds_id}")
    ok = _visible_ids(user, [r["id"] for r in rows if r.get("kind") == "dataset"])
    return [r for r in rows if r.get("kind") != "dataset" or r["id"] in ok]


# ── Parameter sweeps ──────────────────────────────────────────────

@app.get("/sweeps", response_class=HTMLResponse)
async def sweeps_page(request: Request, user: dict = Depends(require_user)):
    return templates.TemplateResponse(request, "sweeps.html", {"current_user": user, "sweeps": _ml("GET", "/sweeps")})


@app.post("/sweeps")
async def sweeps_create(request: Request, user: dict = Depends(require_uploader)):
    form = await request.form()
    ranges = {}
    for p in SCHEMA.parameters:
        if form.get(f"use_{p.name}") and form.get(f"min_{p.name}") not in (None, "") and form.get(f"max_{p.name}") not in (None, ""):
            ranges[p.name] = [float(form[f"min_{p.name}"]), float(form[f"max_{p.name}"])]
    if not ranges:
        raise HTTPException(status_code=400, detail="Select at least one numeric parameter with a range")
    body = {"name": form.get("name") or "Sweep", "description": form.get("description") or None,
            "param_ranges": ranges, "objective_quantity": form.get("objective_quantity") or None,
            "objective_goal": form.get("objective_goal") or None, "created_by": user["username"]}
    sw = _ml("POST", "/sweeps", json=body)
    return RedirectResponse(url=f"/sweeps/{sw['id']}", status_code=303)


@app.get("/sweeps/{sweep_id}", response_class=HTMLResponse)
async def sweep_page(request: Request, sweep_id: str, user: dict = Depends(require_user)):
    sw = _ml("GET", f"/sweeps/{sweep_id}")
    ok = _visible_ids(user, [d["id"] for d in sw["datasets"]])
    sw["datasets"] = [d for d in sw["datasets"] if d["id"] in ok]
    cov = _ml("POST", f"/sweeps/{sweep_id}/coverage")
    return templates.TemplateResponse(request, "sweep_detail.html", {"current_user": user, "sw": sw, "cov": cov})


@app.post("/sweeps/{sweep_id}/link")
async def sweep_link(sweep_id: str, user: dict = Depends(require_uploader), dataset: str = Form(...)):
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT id FROM datasets WHERE id::text = %s OR name = %s OR name LIKE %s", (dataset, dataset, dataset.replace("*", "%")))
    ids = [str(r["id"]) for r in cur.fetchall()]
    conn.close()
    for ds_id in _visible_ids(user, ids):
        _ml("POST", f"/sweeps/{sweep_id}/link", params={"dataset_id": ds_id})
    return RedirectResponse(url=f"/sweeps/{sweep_id}", status_code=303)


@app.post("/sweeps/{sweep_id}/suggest")
async def sweep_suggest(sweep_id: str, user: dict = Depends(require_uploader), n: int = Form(5),
                        method: str = Form("sobol")):
    _ml("POST", f"/sweeps/{sweep_id}/suggest", params={"n": n, "method": method})
    return RedirectResponse(url=f"/sweeps/{sweep_id}", status_code=303)


@app.post("/sweeps/{sweep_id}/suggestions/{sid}/use")
async def sweep_use(sweep_id: str, sid: str, user: dict = Depends(require_uploader), dataset_id: str = Form("")):
    _ml("POST", f"/sweeps/{sweep_id}/suggestions/{sid}/use", params={"dataset_id": dataset_id or None})
    return RedirectResponse(url=f"/sweeps/{sweep_id}", status_code=303)


@app.get("/api/sweeps")
async def api_sweeps(user: dict = Depends(require_user)):
    return _ml("GET", "/sweeps")


@app.post("/api/sweeps")
async def api_sweeps_create(request: Request, user: dict = Depends(require_uploader)):
    body = await request.json()
    body["created_by"] = user["username"]
    return _ml("POST", "/sweeps", json=body)


@app.get("/api/sweeps/{sweep_id}")
async def api_sweep(sweep_id: str, user: dict = Depends(require_user)):
    sw = _ml("GET", f"/sweeps/{sweep_id}")
    ok = _visible_ids(user, [d["id"] for d in sw["datasets"]])
    sw["datasets"] = [d for d in sw["datasets"] if d["id"] in ok]
    sw["coverage"] = _ml("POST", f"/sweeps/{sweep_id}/coverage")
    return sw


@app.post("/api/sweeps/{sweep_id}/link/{ds_id}")
async def api_sweep_link(sweep_id: str, ds_id: str, user: dict = Depends(require_uploader)):
    _get_dataset(ds_id, user)
    return _ml("POST", f"/sweeps/{sweep_id}/link", params={"dataset_id": ds_id})


@app.post("/api/sweeps/{sweep_id}/suggest")
async def api_sweep_suggest(sweep_id: str, n: int = 5, method: str = "sobol", user: dict = Depends(require_uploader)):
    return _ml("POST", f"/sweeps/{sweep_id}/suggest", params={"n": n, "method": method})


# ── Health ────────────────────────────────────────────────────────

# ── Project groups ────────────────────────────────────────────────
# A group is a named set of users (for example the members of one project or
# work package). Datasets list the groups allowed to see them in allowed_groups;
# admins create groups, admins and group managers manage membership.

_GROUP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,62}$")


def _group_row(name: str, cur) -> dict:
    cur.execute("SELECT * FROM datahub_groups WHERE name=%s", (name,))
    g = cur.fetchone()
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    return dict(g)


def _can_manage_group(user: dict, name: str) -> bool:
    return user.get("role") == "admin" or name in (user.get("managed_groups") or [])


def _list_groups(user: dict) -> list[dict]:
    """Groups with members; non-admins see the groups they belong to."""
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute("""SELECT g.id, g.name, g.description, g.created_by, g.created_at,
                          (SELECT count(*) FROM datasets d WHERE g.name = ANY(d.allowed_groups)) AS datasets,
                          coalesce(json_agg(json_build_object('username', u.username, 'organization', u.organization,
                                   'is_manager', m.is_manager) ORDER BY u.username)
                                   FILTER (WHERE u.id IS NOT NULL), '[]') AS members
                   FROM datahub_groups g LEFT JOIN group_members m ON m.group_id = g.id
                   LEFT JOIN datahub_users u ON u.id = m.user_id
                   GROUP BY g.id ORDER BY g.name""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    if user.get("role") != "admin":
        rows = [r for r in rows if r["name"] in (user.get("groups") or [])]
    return rows


@app.get("/groups", response_class=HTMLResponse)
async def groups_page(request: Request, user: dict = Depends(require_user), error: str = ""):
    return templates.TemplateResponse(request, "groups.html", {"current_user": user, "groups": _list_groups(user),
                                                              "error": error})


@app.get("/api/groups")
async def api_groups(user: dict = Depends(require_user)):
    return json.loads(json.dumps(_list_groups(user), default=str))


def _create_group(user: dict, name: str, description: str = "") -> dict:
    name = name.strip()
    if not _GROUP_NAME.match(name):
        raise HTTPException(status_code=400, detail="Group names: 2 to 63 letters, digits, '.', '_' or '-'")
    conn = _get_db_conn(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO datahub_groups (name, description, created_by) VALUES (%s,%s,%s) RETURNING id, name",
                    (name, description.strip() or None, user["username"]))
    except psycopg2.errors.UniqueViolation:
        conn.close()
        raise HTTPException(status_code=409, detail=f"Group '{name}' already exists")
    row = dict(cur.fetchone()); conn.commit(); conn.close()
    return {"id": str(row["id"]), "name": row["name"]}


def _delete_group(name: str):
    conn = _get_db_conn(); cur = conn.cursor()
    g = _group_row(name, cur)
    cur.execute("SELECT count(*) AS n FROM datasets WHERE %s = ANY(allowed_groups)", (name,))
    n = cur.fetchone()["n"]
    if n:
        conn.close()
        # Removing the group would silently widen access to those datasets.
        raise HTTPException(status_code=409, detail=f"Group '{name}' still restricts {n} dataset(s); change their access first")
    cur.execute("DELETE FROM datahub_groups WHERE id=%s", (g["id"],))
    conn.commit(); conn.close()


def _set_member(actor: dict, name: str, username: str, add: bool, manager: Optional[bool] = None):
    if not _can_manage_group(actor, name):
        raise HTTPException(status_code=403, detail="Only admins and group managers can change membership")
    conn = _get_db_conn(); cur = conn.cursor()
    g = _group_row(name, cur)
    cur.execute("SELECT id FROM datahub_users WHERE username=%s", (username.strip(),))
    u = cur.fetchone()
    if not u:
        conn.close()
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    if add:
        if manager is not None and actor.get("role") != "admin":
            manager = None                       # only admins appoint managers
        cur.execute("""INSERT INTO group_members (group_id, user_id, is_manager, added_by) VALUES (%s,%s,%s,%s)
                       ON CONFLICT (group_id, user_id) DO UPDATE SET is_manager =
                       CASE WHEN %s IS NULL THEN group_members.is_manager ELSE %s END""",
                    (g["id"], u["id"], bool(manager), actor["username"], manager, bool(manager)))
    else:
        cur.execute("DELETE FROM group_members WHERE group_id=%s AND user_id=%s", (g["id"], u["id"]))
    conn.commit(); conn.close()


@app.post("/api/groups")
async def api_create_group(request: Request, user: dict = Depends(require_admin)):
    body = await request.json()
    return _create_group(user, body.get("name", ""), body.get("description", ""))


@app.delete("/api/groups/{name}")
async def api_delete_group(name: str, user: dict = Depends(require_admin)):
    _delete_group(name)
    return {"deleted": name}


@app.post("/api/groups/{name}/members")
async def api_add_member(name: str, request: Request, user: dict = Depends(require_user)):
    body = await request.json()
    _set_member(user, name, body.get("username", ""), True, body.get("is_manager"))
    return {"group": name, "added": body.get("username")}


@app.delete("/api/groups/{name}/members/{username}")
async def api_remove_member(name: str, username: str, user: dict = Depends(require_user)):
    _set_member(user, name, username, False)
    return {"group": name, "removed": username}


def _form_redirect(fn, *args, target="/groups", **kw):
    try:
        fn(*args, **kw)
    except HTTPException as exc:
        from urllib.parse import quote
        return RedirectResponse(f"{target}?error={quote(str(exc.detail))}", status_code=303)
    return RedirectResponse(target, status_code=303)


@app.post("/groups")
async def groups_create_form(user: dict = Depends(require_admin), name: str = Form(...), description: str = Form("")):
    return _form_redirect(_create_group, user, name, description)


@app.post("/groups/{name}/delete")
async def groups_delete_form(name: str, user: dict = Depends(require_admin)):
    return _form_redirect(_delete_group, name)


@app.post("/groups/{name}/members")
async def groups_member_form(name: str, user: dict = Depends(require_user), username: str = Form(...),
                             action: str = Form("add"), is_manager: str = Form("")):
    manager = None if is_manager == "" else is_manager in ("1", "true", "on")
    return _form_redirect(_set_member, user, name, username, action == "add", manager)


# ── Dataset access (organisations and groups) ─────────────────────

def _set_access(user: dict, ds_id: str, orgs: Optional[list[str]], groups: Optional[list[str]]) -> dict:
    ds = _get_dataset(ds_id, user)
    if not _user_can_delete(user, ds.get("uploaded_by")):
        raise HTTPException(status_code=403, detail="Only the uploader or an admin can change access")
    new_orgs = ds["allowed_orgs"] if orgs is None else sorted({o.strip() for o in orgs if o.strip()})
    if any(not _valid_org(o) for o in new_orgs):
        raise HTTPException(status_code=400, detail="Unknown organisation")
    if groups is None:
        new_groups = ds["allowed_groups"]
    else:
        wanted = sorted({g.strip() for g in groups if g.strip()})
        added = [g for g in wanted if g not in (ds["allowed_groups"] or [])]
        _check_groups(user, added)          # keeping existing groups is always allowed
        if wanted:
            _check_groups({"role": "admin"}, wanted)
        new_groups = wanted
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute("UPDATE datasets SET allowed_orgs=%s, allowed_groups=%s WHERE id=%s", (new_orgs, new_groups, ds_id))
    conn.commit(); conn.close()
    return {"dataset_id": ds_id, "allowed_orgs": new_orgs, "allowed_groups": new_groups}


@app.patch("/api/datasets/{ds_id}/access")
async def api_set_access(ds_id: str, request: Request, user: dict = Depends(require_user)):
    body = await request.json()
    return _set_access(user, ds_id, body.get("allowed_orgs"), body.get("allowed_groups"))


@app.post("/dataset/{ds_id}/access")
async def dataset_access_form(ds_id: str, request: Request, user: dict = Depends(require_user)):
    form = await request.form()
    return _form_redirect(_set_access, user, ds_id, form.getlist("org"), form.getlist("group"),
                          target=f"/dataset/{ds_id}")


# ── Time-limited sharing links ────────────────────────────────────
# A link gives read access to one dataset without an account, until it expires,
# reaches its download limit or is revoked. Only the SHA-256 of the token is
# stored; the full link is shown once, at creation.

import hashlib as _hashlib
import secrets as _secrets
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

SHARE_MAX_DAYS = float(os.environ.get("DATAHUB_SHARE_MAX_DAYS", "30") or 0)
PUBLIC_URL = os.environ.get("DATAHUB_PUBLIC_URL", "").rstrip("/")


def _token_hash(token: str) -> str:
    return _hashlib.sha256(token.encode()).hexdigest()


def _share_status(row: dict) -> str:
    now = _dt.now(_tz.utc)
    if row.get("revoked_at"):
        return "revoked"
    if row["expires_at"] <= now:
        return "expired"
    if row.get("max_downloads") and row["download_count"] >= row["max_downloads"]:
        return "used up"
    return "active"


def _create_share(user: dict, ds_id: str, base_url: str, days: float = 7, max_downloads: Optional[int] = None,
                  note: str = "") -> dict:
    if SHARE_MAX_DAYS <= 0:
        raise HTTPException(status_code=403, detail="Sharing links are disabled on this instance")
    ds = _get_dataset(ds_id, user)
    if not _user_can_delete(user, ds.get("uploaded_by")):
        raise HTTPException(status_code=403, detail="Only the uploader or an admin can create sharing links")
    try:
        days = float(days)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="days must be a number")
    if not 0 < days <= SHARE_MAX_DAYS:
        raise HTTPException(status_code=400, detail=f"Lifetime must be more than 0 and at most {SHARE_MAX_DAYS:g} days")
    if max_downloads in ("", None):
        max_downloads = None
    else:
        max_downloads = int(max_downloads)
        if max_downloads < 1:
            raise HTTPException(status_code=400, detail="max_downloads must be at least 1")
    hidden = sorted(_ORG_HIDDEN_TYPES.get(user.get("organization") or "", set())) if user.get("role") != "admin" else []
    token = _secrets.token_urlsafe(32)
    expires = _dt.now(_tz.utc) + _td(days=days)
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute("""INSERT INTO share_links (token_hash, dataset_id, created_by, expires_at, max_downloads, hidden_classes, note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (_token_hash(token), ds_id, user["username"], expires, max_downloads, hidden, (note or "").strip()[:200] or None))
    sid = str(cur.fetchone()["id"]); conn.commit(); conn.close()
    base = PUBLIC_URL or base_url.rstrip("/")
    return {"id": sid, "url": f"{base}/s/{token}", "expires_at": expires.isoformat(), "max_downloads": max_downloads}


def _list_shares(user: dict, ds_id: Optional[str] = None) -> list[dict]:
    conn = _get_db_conn(); cur = conn.cursor()
    sql = """SELECT s.id, s.dataset_id, d.name AS dataset, s.created_by, s.created_at, s.expires_at, s.max_downloads,
                    s.download_count, s.note, s.revoked_at, s.revoked_by, s.last_used_at, d.uploaded_by
             FROM share_links s JOIN datasets d ON d.id = s.dataset_id"""
    if ds_id:
        cur.execute(sql + " WHERE s.dataset_id = %s ORDER BY s.created_at DESC", (ds_id,))
    else:
        cur.execute(sql + " ORDER BY s.created_at DESC LIMIT 500")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    if user.get("role") != "admin":
        rows = [r for r in rows if user["username"] in (r["created_by"], r["uploaded_by"])]
    for r in rows:
        r["status"] = _share_status(r)
        r.pop("uploaded_by", None)
    return rows


def _revoke_share(user: dict, share_id: str):
    try:
        uuid_module.UUID(share_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Link not found")
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute("""SELECT s.created_by, d.uploaded_by, s.dataset_id FROM share_links s
                   JOIN datasets d ON d.id = s.dataset_id WHERE s.id=%s""", (share_id,))
    row = cur.fetchone()
    if not row or (user.get("role") != "admin" and user["username"] not in (row["created_by"], row["uploaded_by"])):
        conn.close()
        raise HTTPException(status_code=404, detail="Link not found")
    cur.execute("UPDATE share_links SET revoked_at = coalesce(revoked_at, NOW()), revoked_by = coalesce(revoked_by, %s) WHERE id=%s",
                (user["username"], share_id))
    conn.commit(); conn.close()
    return str(row["dataset_id"])


@app.post("/api/datasets/{ds_id}/shares")
async def api_create_share(ds_id: str, request: Request, user: dict = Depends(require_user)):
    body = await request.json()
    return _create_share(user, ds_id, str(request.base_url), body.get("days", 7), body.get("max_downloads"),
                         body.get("note", ""))


@app.get("/api/datasets/{ds_id}/shares")
async def api_list_shares(ds_id: str, user: dict = Depends(require_user)):
    _get_dataset(ds_id, user)
    return json.loads(json.dumps(_list_shares(user, ds_id), default=str))


@app.delete("/api/shares/{share_id}")
async def api_revoke_share(share_id: str, user: dict = Depends(require_user)):
    _revoke_share(user, share_id)
    return {"revoked": share_id}


@app.get("/api/admin/shares")
async def api_admin_shares(user: dict = Depends(require_admin)):
    return json.loads(json.dumps(_list_shares(user), default=str))


@app.post("/dataset/{ds_id}/shares")
async def dataset_share_form(ds_id: str, request: Request, user: dict = Depends(require_user),
                             days: str = Form("7"), max_downloads: str = Form(""), note: str = Form("")):
    try:
        link = _create_share(user, ds_id, str(request.base_url), days, max_downloads or None, note)
    except HTTPException as exc:
        from urllib.parse import quote
        return RedirectResponse(f"/dataset/{ds_id}?share_error={quote(str(exc.detail))}#sharing", status_code=303)
    request.session["new_share_url"] = link["url"]      # shown once on the dataset page
    return RedirectResponse(f"/dataset/{ds_id}#sharing", status_code=303)


@app.post("/shares/{share_id}/revoke")
async def share_revoke_form(share_id: str, request: Request, user: dict = Depends(require_user)):
    ds_id = _revoke_share(user, share_id)
    back = request.headers.get("referer") or f"/dataset/{ds_id}"
    return RedirectResponse(back if back.startswith(str(request.base_url)) else f"/dataset/{ds_id}", status_code=303)


@app.get("/admin/shares", response_class=HTMLResponse)
async def admin_shares_page(request: Request, user: dict = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin_shares.html", {"current_user": user, "shares": _list_shares(user),
                                                                    "max_days": SHARE_MAX_DAYS})


# public side: no login, the token is the credential

def _resolve_share(token: str, request: Request, count: bool = False) -> tuple[dict, dict]:
    ip = request.client.host if request.client else "unknown"
    if _check_login_rate(f"share:{ip}"):
        raise HTTPException(status_code=429, detail="Too many invalid links; try again later")
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", token or ""):
        _record_login_failure(f"share:{ip}")
        raise HTTPException(status_code=404, detail="This link is not valid")
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM share_links WHERE token_hash=%s", (_token_hash(token),))
    link = cur.fetchone()
    if not link:
        conn.close(); _record_login_failure(f"share:{ip}")
        raise HTTPException(status_code=404, detail="This link is not valid")
    link = dict(link)
    status = _share_status(link)
    if status != "active":
        conn.close()
        raise HTTPException(status_code=410, detail=f"This link is {status}")
    if count:
        # atomic: never exceed max_downloads under concurrent requests
        cur.execute("""UPDATE share_links SET download_count = download_count + 1, last_used_at = NOW()
                       WHERE id=%s AND revoked_at IS NULL AND expires_at > NOW()
                       AND (max_downloads IS NULL OR download_count < max_downloads) RETURNING id""", (link["id"],))
        if not cur.fetchone():
            conn.rollback(); conn.close()
            raise HTTPException(status_code=410, detail="This link is used up or expired")
    else:
        cur.execute("UPDATE share_links SET last_used_at = NOW() WHERE id=%s", (link["id"],))
    conn.commit(); conn.close()
    # Show the dataset as an admin would, minus the classes hidden from the link creator.
    ds = _get_dataset(str(link["dataset_id"]), {"role": "admin"})
    hidden = set(link["hidden_classes"] or [])
    ds["files"] = [f for f in ds["files"] if f["file_class"] not in hidden]
    for p in ds["parts"]:
        p["files"] = [f for f in p["files"] if f["file_class"] not in hidden]
    return link, ds


def _log_share_download(link: dict, row: dict):
    try:
        conn = _get_db_conn()
        conn.cursor().execute("""INSERT INTO download_logs (user_id, username, dataset_id, file_id, object_key, filename,
                                 size_bytes, share_link_id) VALUES (NULL,%s,%s,%s,%s,%s,%s,%s)""",
                              (f"link:{str(link['id'])[:8]} by {link['created_by']}", row.get("dataset_id"), row.get("id"),
                               row.get("object_key"), row.get("filename"), row.get("size_bytes"), str(link["id"])))
        conn.commit(); conn.close()
    except Exception as exc:
        logger.warning("download log failed: %s", exc)


def _public_meta(ds: dict, link: dict) -> dict:
    keep = ("id", "name", "dataset_type", "status", "parameters", "metadata", "tags", "notes", "created_at",
            "started_at", "finished_at", "quantities")
    out = {k: ds.get(k) for k in keep}
    files = ds["files"] + [f for p in ds["parts"] for f in p["files"]]
    out["files"] = [{k: f[k] for k in ("id", "relpath", "file_class", "size_bytes", "sha256", "time_value")} for f in files]
    out["link"] = {"expires_at": link["expires_at"], "max_downloads": link["max_downloads"],
                   "download_count": link["download_count"]}
    return json.loads(json.dumps(out, default=str))


@app.get("/s/{token}", response_class=HTMLResponse)
async def share_page(token: str, request: Request):
    try:
        link, ds = _resolve_share(token, request)
    except HTTPException as exc:
        return templates.TemplateResponse(request, "share.html", {"error": exc.detail}, status_code=exc.status_code)
    return templates.TemplateResponse(request, "share.html", {
        "ds": ds, "link": link, "token": token, "status": _share_status(link),
        "class_labels": {c.id: c.label for c in SCHEMA.file_classes} | {"other": "Other"},
        "qlabels": {q.name: q for q in SCHEMA.quantities}, "plabels": {p.name: p for p in SCHEMA.parameters},
        "dtype": next((t for t in SCHEMA.dataset_types if t.id == ds["dataset_type"]), None)})


@app.get("/s/{token}/meta")
async def share_meta(token: str, request: Request):
    link, ds = _resolve_share(token, request)
    return _public_meta(ds, link)


@app.get("/s/{token}/files/{file_id}")
async def share_file(token: str, file_id: str, request: Request):
    link, ds = _resolve_share(token, request)
    files = {str(f["id"]): f for f in ds["files"] + [f for p in ds["parts"] for f in p["files"]]}
    try:
        f = files.get(str(uuid_module.UUID(file_id)))
    except ValueError:
        f = None
    if f is None:
        raise HTTPException(status_code=404, detail="File not found in this link")
    link, _ = _resolve_share(token, request, count=True)
    conn = _get_db_conn(); cur = conn.cursor()
    cur.execute("SELECT object_key FROM files WHERE id=%s", (str(f["id"]),))
    key = cur.fetchone()["object_key"]; conn.close()
    _log_share_download(link, {"dataset_id": str(ds["id"]), "id": str(f["id"]), "object_key": key,
                               "filename": f["filename"], "size_bytes": f["size_bytes"]})
    obj = _get_minio().get_object(MINIO_BUCKET, key)

    def stream():
        try:
            for chunk in obj.stream(1 << 16):
                yield chunk
        finally:
            obj.close(); obj.release_conn()
    ctype = mimetypes.guess_type(f["filename"])[0] or "application/octet-stream"
    return StreamingResponse(stream(), media_type=ctype, headers={
        "Content-Disposition": _content_disposition(f["filename"]), "Content-Length": str(f["size_bytes"] or "")})


@app.get("/s/{token}/archive.zip")
async def share_archive(token: str, request: Request):
    link, ds = _resolve_share(token, request, count=True)
    files = ds["files"] + [f for p in ds["parts"] for f in p["files"]]

    def build() -> str:
        mc = _get_minio(); conn = _get_db_conn(); cur = conn.cursor()
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{ds['name']}/datahub_metadata.json", json.dumps(_public_meta(ds, link), indent=2))
            for f in files:
                cur.execute("SELECT object_key FROM files WHERE id=%s", (str(f["id"]),))
                obj = mc.get_object(MINIO_BUCKET, cur.fetchone()["object_key"])
                with zf.open(f"{ds['name']}/{f['relpath']}", "w") as out:
                    for chunk in obj.stream(1 << 16):
                        out.write(chunk)
                obj.close(); obj.release_conn()
        conn.close()
        return tmp.name
    path = await run_in_threadpool(build)
    _log_share_download(link, {"dataset_id": str(ds["id"]), "filename": f"{ds['name']}.zip",
                               "size_bytes": os.path.getsize(path)})

    def stream():
        try:
            with open(path, "rb") as fh:
                while chunk := fh.read(1 << 16):
                    yield chunk
        finally:
            os.unlink(path)
    return StreamingResponse(stream(), media_type="application/zip",
                             headers={"Content-Disposition": _content_disposition(f"{ds['name']}.zip")})


# ── API documentation (logged-in users only) ─────────────────────

@app.get("/api/openapi.json", include_in_schema=False)
async def api_openapi(user: dict = Depends(require_user)):
    return JSONResponse(app.openapi())


@app.get("/api/docs", include_in_schema=False)
async def api_docs(user: dict = Depends(require_user)):
    return get_swagger_ui_html(openapi_url="/api/openapi.json", title="DataHub API",
                               swagger_js_url="/static/vendor/swagger-ui/swagger-ui-bundle.js",
                               swagger_css_url="/static/vendor/swagger-ui/swagger-ui.css",
                               swagger_favicon_url="/static/vendor/swagger-ui/favicon-32x32.png")


@app.get("/health")
async def health():
    return {"status": "ok", "schema": SCHEMA.hash}
