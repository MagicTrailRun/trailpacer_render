import os
import json
import secrets
import hashlib
import base64
import string
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, send_file, abort, Response, request, redirect

BASE_DIR = Path(__file__).parent
PUBLIC_DIR = BASE_DIR / "public"
PLANS_DIR  = BASE_DIR / "plans"

app = Flask(__name__, static_folder=str(PUBLIC_DIR), static_url_path="")

SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

STRAVA_CLIENT_ID     = os.environ.get("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
GARMIN_CLIENT_ID     = os.environ.get("GARMIN_CLIENT_ID", "")
GARMIN_CLIENT_SECRET = os.environ.get("GARMIN_CLIENT_SECRET", "")

BACKEND_STRAVA_IS_LINKED_URL      = os.environ.get("BACKEND_STRAVA_IS_LINKED_URL", "")
BACKEND_GARMIN_IS_LINKED_URL      = os.environ.get("BACKEND_GARMIN_IS_LINKED_URL", "")
BACKEND_STRAVA_DEREGISTRATION_URL = os.environ.get("BACKEND_STRAVA_DEREGISTRATION_URL", "")
BACKEND_GARMIN_DEREGISTRATION_URL = os.environ.get("BACKEND_GARMIN_DEREGISTRATION_URL", "")

# Temporary PKCE store (in-memory, fine for single-instance Render)
_pkce_store: dict[str, str] = {}


# ── Helpers ───────────────────────────────────────────────────

def _app_base_url():
    """Root URL of this deployment."""
    if os.environ.get("RENDER_EXTERNAL_URL"):
        return os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
    return request.host_url.rstrip("/")

def _supabase_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON_KEY}

def _supabase_admin_headers() -> dict:
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
    return {"Authorization": f"Bearer {key}", "apikey": key,
            "Content-Type": "application/json"}

def _get_supabase_user(bearer_token: str) -> dict | None:
    """Verify a Supabase JWT and return the user object, or None."""
    if not bearer_token or not SUPABASE_URL:
        return None
    r = requests.get(
        f"{SUPABASE_URL}/auth/v1/user",
        headers=_supabase_headers(bearer_token),
        timeout=5
    )
    return r.json() if r.status_code == 200 else None

def _save_integration(user_id: str, platform: str, tokens: dict):
    now = datetime.now(timezone.utc).isoformat()
    expires_raw = tokens.get("expires_at")
    if isinstance(expires_raw, (int, float)):
        expires_iso = datetime.fromtimestamp(expires_raw, tz=timezone.utc).isoformat()
    else:
        expires_iso = now

    payload = {
        "user_id": str(user_id),
        "provider": platform,
        "external_user_id": tokens.get("external_id"),
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": expires_iso,
        "connected_at": now,
    }
    requests.post(
        f"{SUPABASE_URL}/rest/v1/integrations",
        headers={**_supabase_admin_headers(),
                 "Prefer": "resolution=merge-duplicates"},
        json=payload,
        timeout=5
    )

def _create_user_profile(user_id: str, email: str):
    now = datetime.now(timezone.utc).isoformat()
    requests.post(
        f"{SUPABASE_URL}/rest/v1/users",
        headers={**_supabase_admin_headers(), "Prefer": "resolution=merge-duplicates"},
        json={"id": user_id, "email": email, "created_at": now, "updated_at": now},
        timeout=5
    )

def _send_deregistration_strava(user_id: str):
    if not BACKEND_STRAVA_DEREGISTRATION_URL:
        return
    try:
        requests.post(
            BACKEND_STRAVA_DEREGISTRATION_URL,
            json={"owner_id": user_id, "object_id": user_id,
                  "object_type": "athlete", "aspect_type": "delete"},
            timeout=10
        )
    except Exception:
        pass

def _send_deregistration_garmin(user_id: str):
    if not BACKEND_GARMIN_DEREGISTRATION_URL:
        return
    try:
        requests.post(
            BACKEND_GARMIN_DEREGISTRATION_URL,
            json={"deregistrations": [{"userId": user_id}]},
            timeout=10
        )
    except Exception:
        pass

def _send_connection_webhook(user_id: str, access_token: str, device: str):
    url = BACKEND_STRAVA_IS_LINKED_URL if device == "strava" else BACKEND_GARMIN_IS_LINKED_URL
    if not url:
        return
    try:
        requests.post(url, json={"userId": user_id, "access_token": access_token}, timeout=5)
    except Exception:
        pass

# PKCE helpers (Garmin)
def _pkce_verifier(length: int = 64) -> str:
    chars = string.ascii_letters + string.digits + "-._~"
    return "".join(secrets.choice(chars) for _ in range(length))

def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


# ── Config JS ─────────────────────────────────────────────────

@app.route("/supabase-config.js")
def supabase_config():
    js = (f"window.SUPABASE_URL='{SUPABASE_URL}';\n"
          f"window.SUPABASE_ANON_KEY='{SUPABASE_ANON_KEY}';\n")
    return Response(js, mimetype="application/javascript")


# ── Pacing files API ──────────────────────────────────────────

@app.route("/api/files")
def file_list():
    files = []
    if PLANS_DIR.exists():
        for folder in sorted(PLANS_DIR.iterdir()):
            if not folder.is_dir():
                continue
            for f in folder.iterdir():
                if f.name.startswith("predict_") and f.name.endswith(".html"):
                    stat = f.stat()
                    files.append({
                        "name":     f.name,
                        "folder":   folder.name,
                        "path":     f"{folder.name}/{f.name}",
                        "size":     stat.st_size,
                        "modified": stat.st_mtime * 1000,
                    })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify(files)

@app.route("/pacing/<path:subpath>")
def serve_pacing(subpath):
    filepath = (PLANS_DIR / subpath).resolve()
    # Prevent path traversal
    if not str(filepath).startswith(str(PLANS_DIR.resolve())):
        abort(403)
    if not filepath.name.startswith("predict_") or not filepath.name.endswith(".html"):
        abort(403)
    if not filepath.exists():
        abort(404)
    return send_file(filepath, mimetype="text/html")


# ── Strava OAuth ──────────────────────────────────────────────

@app.route("/api/strava/authorize")
def strava_authorize():
    redirect_uri = _app_base_url() + "/"
    auth_url = (
        "https://www.strava.com/oauth/authorize"
        f"?client_id={STRAVA_CLIENT_ID}"
        "&response_type=code"
        f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
        "&scope=read,profile:read_all,activity:read_all"
        "&approval_prompt=force"
        "&state=strava"
    )
    return redirect(auth_url)


@app.route("/api/strava/callback", methods=["POST"])
def strava_callback():
    data = request.get_json(force=True)
    code  = data.get("code")
    token = data.get("token")

    user = _get_supabase_user(token)
    if not user:
        return jsonify({"error": "Non authentifié"}), 401

    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=10)
    if r.status_code != 200:
        return jsonify({"error": "Échec du token Strava"}), 400
    tokens = r.json()

    athlete_r = requests.get(
        "https://www.strava.com/api/v3/athlete",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        timeout=5
    )
    if athlete_r.status_code != 200:
        return jsonify({"error": "Échec récupération athlète"}), 400
    athlete = athlete_r.json()

    _save_integration(user["id"], "strava", {
        "external_id": str(athlete["id"]),
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": tokens["expires_at"],
    })
    _send_connection_webhook(str(athlete["id"]), tokens["access_token"], "strava")

    return jsonify({"ok": True, "name": athlete.get("firstname", "")})


# ── Garmin OAuth (PKCE) ───────────────────────────────────────

@app.route("/api/garmin/authorize")
def garmin_authorize():
    verifier   = _pkce_verifier()
    challenge  = _pkce_challenge(verifier)
    state_key  = secrets.token_urlsafe(16)
    _pkce_store[state_key] = verifier          # store until callback

    redirect_uri = _app_base_url() + "/"
    auth_url = (
        "https://connect.garmin.com/oauth2Confirm"
        f"?client_id={GARMIN_CLIENT_ID}"
        "&response_type=code"
        f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
        f"&code_challenge={challenge}"
        "&code_challenge_method=S256"
        f"&state=garmin_{state_key}"
    )
    return redirect(auth_url)


@app.route("/api/garmin/callback", methods=["POST"])
def garmin_callback():
    data      = request.get_json(force=True)
    code      = data.get("code")
    state_key = data.get("state_key")   # the random suffix after "garmin_"
    token     = data.get("token")

    user = _get_supabase_user(token)
    if not user:
        return jsonify({"error": "Non authentifié"}), 401

    verifier = _pkce_store.pop(state_key, None)
    if not verifier:
        return jsonify({"error": "Session PKCE expirée, recommencez"}), 400

    redirect_uri = _app_base_url() + "/"
    r = requests.post(
        "https://diauth.garmin.com/di-oauth2-service/oauth/token",
        data={
            "client_id": GARMIN_CLIENT_ID,
            "client_secret": GARMIN_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }, timeout=10
    )
    if r.status_code != 200:
        return jsonify({"error": "Échec du token Garmin"}), 400
    tokens = r.json()
    access_token = tokens.get("access_token")

    user_r = requests.get(
        "https://apis.garmin.com/wellness-api/rest/user/id",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=5
    )
    if user_r.status_code != 200:
        return jsonify({"error": "Échec récupération userId Garmin"}), 400
    garmin_user_id = user_r.json().get("userId")

    expires_in = tokens.get("expires_in", 3600)
    expires_at = datetime.now(timezone.utc).timestamp() + expires_in - 600

    _save_integration(user["id"], "garmin", {
        "external_id": garmin_user_id,
        "access_token": access_token,
        "refresh_token": tokens.get("refresh_token"),
        "expires_at": expires_at,
    })
    _send_connection_webhook(garmin_user_id, access_token, "garmin")

    return jsonify({"ok": True})


# ── Disconnect ───────────────────────────────────────────────

@app.route("/api/strava/disconnect", methods=["POST"])
def strava_disconnect():
    data  = request.get_json(force=True)
    token = data.get("token")
    user  = _get_supabase_user(token)
    if not user:
        return jsonify({"error": "Non authentifié"}), 401

    # Récupère le token Strava en base pour révoquer côté Strava
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/integrations",
        headers={**_supabase_admin_headers(), "Accept": "application/json"},
        params={"user_id": f"eq.{user['id']}", "provider": "eq.strava",
                "select": "access_token,external_user_id"},
        timeout=5
    )
    if r.status_code == 200 and r.json():
        row = r.json()[0]
        access_token = row.get("access_token")
        external_user_id = row.get("external_user_id")
        if access_token:
            try:
                requests.post(
                    "https://www.strava.com/oauth/deauthorize",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=5
                )
            except Exception:
                pass
        if external_user_id:
            _send_deregistration_strava(external_user_id)

    return jsonify({"ok": True})


@app.route("/api/garmin/disconnect", methods=["POST"])
def garmin_disconnect():
    data  = request.get_json(force=True)
    token = data.get("token")
    user  = _get_supabase_user(token)
    if not user:
        return jsonify({"error": "Non authentifié"}), 401

    # Récupère le token Garmin pour révoquer côté Garmin
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/integrations",
        headers={**_supabase_admin_headers(), "Accept": "application/json"},
        params={"user_id": f"eq.{user['id']}", "provider": "eq.garmin",
                "select": "access_token,external_user_id"},
        timeout=5
    )
    if r.status_code == 200 and r.json():
        row = r.json()[0]
        access_token = row.get("access_token")
        external_user_id = row.get("external_user_id")
        if access_token:
            try:
                requests.delete(
                    "https://apis.garmin.com/wellness-api/rest/user/registration",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=5
                )
            except Exception:
                pass
        if external_user_id:
            _send_deregistration_garmin(external_user_id)

    return jsonify({"ok": True})


# ── User registration ─────────────────────────────────────────

@app.route("/api/user/register", methods=["POST"])
def register_user():
    data  = request.get_json(force=True)
    token = data.get("token")
    user  = _get_supabase_user(token)
    if not user:
        return jsonify({"error": "Non authentifié"}), 401
    _create_user_profile(user["id"], user.get("email", ""))
    return jsonify({"ok": True})


# ── Integrations status ───────────────────────────────────────

@app.route("/api/integrations")
def get_integrations():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    user  = _get_supabase_user(token)
    if not user:
        return jsonify({"strava": False, "garmin": False})

    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/integrations",
        headers={**_supabase_admin_headers(),
                 "Accept": "application/json"},
        params={"user_id": f"eq.{user['id']}", "select": "provider"},
        timeout=5
    )
    result = {"strava": False, "garmin": False}
    if r.status_code == 200:
        for row in r.json():
            if row["provider"] in result:
                result[row["provider"]] = True
    return jsonify(result)


# ── Static / SPA ──────────────────────────────────────────────

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def static_files(path):
    return app.send_static_file("index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
