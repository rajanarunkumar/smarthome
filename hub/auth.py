"""Sign-in: Google (OpenID Connect) and passkeys (Face ID / Touch ID), with cookie sessions.

Flow
  1. First visit on a device: Sign in with Google. Only allow-listed emails get in.
  2. The app then offers to add a passkey, so later sign-ins are one Face ID tap.
  3. Sessions last `session_hours` (default 12); after that, Face ID again.

Scripts and voice integrations (Siri Shortcuts, later Alexa/Google) use the
separate API token instead, because they can't do Face ID.

Config lives in ~/.smarthome/secrets.json under "auth":
  google_client_id, allowed_emails, origins, session_hours
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import jwt
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .config import Secrets
from .models import HubError

SESSION_COOKIE = "hub_session"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]
CHALLENGE_SECONDS = 300


@dataclass
class Pending:
    kind: str           # "google", "passkey-login", "passkey-register"
    value: bytes | str  # nonce or WebAuthn challenge
    origin: str
    email: str | None
    expires: float


class Auth:
    def __init__(self, secrets_store: Secrets, db: sqlite3.Connection) -> None:
        self.secrets = secrets_store
        self.db = db
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                email      TEXT NOT NULL,
                method     TEXT NOT NULL,
                created    INTEGER NOT NULL,
                expires    INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passkeys (
                id          TEXT PRIMARY KEY,
                email       TEXT NOT NULL,
                rp_id       TEXT NOT NULL,
                public_key  BLOB NOT NULL,
                sign_count  INTEGER NOT NULL,
                label       TEXT NOT NULL,
                created     INTEGER NOT NULL,
                last_used   INTEGER
            );
            CREATE TABLE IF NOT EXISTS app_tokens (
                token_hash TEXT PRIMARY KEY,
                email      TEXT NOT NULL,
                label      TEXT NOT NULL,
                created    INTEGER NOT NULL,
                last_used  INTEGER
            );
            """
        )
        self.db.commit()
        self._pending: dict[str, Pending] = {}
        self._jwks = jwt.PyJWKClient(GOOGLE_CERTS_URL, cache_keys=True, lifespan=3600)

    # Config
    @property
    def config(self) -> dict:
        return self.secrets.get("auth") or {}

    @property
    def origins(self) -> list[str]:
        return self.config.get("origins", [])

    @property
    def session_seconds(self) -> int:
        return int(self.config.get("session_hours", 12) * 3600)

    def check_origin(self, origin: str | None) -> str:
        if not origin or origin not in self.origins:
            raise HubError(403, "This address isn't allowed to sign in.")
        return origin

    @staticmethod
    def rp_id(origin: str) -> str:
        return urlparse(origin).hostname or ""

    def _allowed(self, email: str) -> bool:
        return email.lower() in {e.lower() for e in self.config.get("allowed_emails", [])}

    # Pending challenges
    def _put(self, pending: Pending) -> str:
        now = time.time()
        for key in [k for k, p in self._pending.items() if p.expires < now]:
            del self._pending[key]
        key = secrets.token_urlsafe(16)
        self._pending[key] = pending
        return key

    def _take(self, key: str, kind: str) -> Pending:
        pending = self._pending.pop(key, None)
        if pending is None or pending.kind != kind or pending.expires < time.time():
            raise HubError(400, "That sign-in attempt expired. Please try again.")
        return pending

    # Sessions
    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_session(self, email: str, method: str) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        self.db.execute(
            "INSERT INTO sessions (token_hash, email, method, created, expires) VALUES (?, ?, ?, ?, ?)",
            (self._hash(token), email, method, now, now + self.session_seconds),
        )
        self.db.commit()
        return token

    def session(self, token: str | None) -> dict | None:
        if not token:
            return None
        row = self.db.execute(
            "SELECT email, method, expires FROM sessions WHERE token_hash = ?", (self._hash(token),)
        ).fetchone()
        if row is None or row[2] < time.time():
            return None
        if not self._allowed(row[0]):  # removed from the allow-list since signing in
            return None
        return {"email": row[0], "method": row[1], "expires": row[2]}

    def end_session(self, token: str | None) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (self._hash(token),))
            self.db.commit()

    # Native app: a signed-in web session hands a one-time code to the iPhone app
    # (PKCE-protected), which trades it for a long-lived, revocable device token.
    APP_REDIRECT = "smarthome://signed-in"

    def app_code(self, email: str, code_challenge: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge or ""):
            raise HubError(400, "Bad app sign-in request.")
        return self._put(Pending("app-code", code_challenge, "", email, time.time() + 120))

    def app_token(self, code: str, code_verifier: str, label: str) -> tuple[str, str]:
        pending = self._take(code, "app-code")
        digest = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(digest, str(pending.value)):
            raise HubError(401, "App sign-in check failed. Please try again.")
        if not self._allowed(pending.email or ""):
            raise HubError(403, "This Google account isn't allowed.")
        token = "app_" + secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO app_tokens (token_hash, email, label, created) VALUES (?, ?, ?, ?)",
            (self._hash(token), pending.email, label[:60] or "iPhone app", int(time.time())),
        )
        self.db.commit()
        return token, pending.email or ""

    def app_user(self, token: str) -> dict | None:
        token_hash = self._hash(token)
        row = self.db.execute("SELECT email FROM app_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
        if row is None or not self._allowed(row[0]):
            return None
        self.db.execute("UPDATE app_tokens SET last_used = ? WHERE token_hash = ?", (int(time.time()), token_hash))
        self.db.commit()
        return {"email": row[0], "method": "ios-app"}

    def app_tokens(self, email: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT token_hash, label, created, last_used FROM app_tokens WHERE email = ? ORDER BY created", (email,)
        ).fetchall()
        return [{"id": r[0][:16], "label": r[1], "created": r[2], "last_used": r[3]} for r in rows]

    def revoke_app_token(self, email: str, token_id: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{16}", token_id):
            raise HubError(404, "Unknown app")
        self.db.execute("DELETE FROM app_tokens WHERE email = ? AND token_hash LIKE ?", (email, token_id + "%"))
        self.db.commit()

    # Google
    def google_start(self, origin: str) -> str:
        client_id = self.config.get("google_client_id")
        if not client_id:
            raise HubError(409, "Google sign-in isn't configured on the hub.")
        nonce = secrets.token_urlsafe(16)
        state = self._put(Pending("google", nonce, origin, None, time.time() + CHALLENGE_SECONDS))
        query = urlencode({
            "client_id": client_id,
            "redirect_uri": origin + "/",
            "response_type": "id_token",
            "scope": "openid email",
            "nonce": nonce,
            "state": state,
            "prompt": "select_account",
        })
        return f"{GOOGLE_AUTH_URL}?{query}"

    async def google_finish(self, origin: str, id_token: str, state: str) -> str:
        pending = self._take(state, "google")
        if pending.origin != origin:
            raise HubError(400, "Sign-in started on a different address.")
        try:
            key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, id_token)
            claims = jwt.decode(
                id_token, key.key, algorithms=["RS256"],
                audience=self.config["google_client_id"], issuer=GOOGLE_ISSUERS, leeway=60,
            )
        except jwt.PyJWTError as exc:
            raise HubError(401, f"Google sign-in couldn't be verified ({exc}).") from exc
        if claims.get("nonce") != pending.value:
            raise HubError(401, "Google sign-in couldn't be verified (nonce).")
        email = claims.get("email", "")
        if not claims.get("email_verified") or not self._allowed(email):
            raise HubError(403, f"{email or 'This account'} isn't allowed to use this hub.")
        return email.lower()

    # Passkeys
    def passkeys(self, email: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, rp_id, label, created, last_used FROM passkeys WHERE email = ? ORDER BY created",
            (email,),
        )
        return [dict(zip(("id", "rp_id", "label", "created", "last_used"), r)) for r in rows]

    def delete_passkey(self, email: str, passkey_id: str) -> None:
        self.db.execute("DELETE FROM passkeys WHERE id = ? AND email = ?", (passkey_id, email))
        self.db.commit()

    def register_options(self, email: str, origin: str) -> dict:
        rp_id = self.rp_id(origin)
        existing = self.db.execute(
            "SELECT id FROM passkeys WHERE email = ? AND rp_id = ?", (email, rp_id)
        ).fetchall()
        options = generate_registration_options(
            rp_id=rp_id,
            rp_name="Home",
            user_name=email,
            user_display_name=email,
            user_id=hashlib.sha256(email.encode()).digest()[:16],
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(r[0])) for r in existing],
        )
        key = self._put(Pending("passkey-register", options.challenge, origin, email, time.time() + CHALLENGE_SECONDS))
        return {"challenge_id": key, "options": json.loads(options_to_json(options))}

    def register_finish(self, email: str, origin: str, challenge_id: str, credential: dict, label: str) -> None:
        pending = self._take(challenge_id, "passkey-register")
        if pending.email != email or pending.origin != origin:
            raise HubError(400, "Passkey setup started in a different session.")
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=pending.value,
                expected_rp_id=self.rp_id(origin),
                expected_origin=origin,
                require_user_verification=True,
            )
        except Exception as exc:  # noqa: BLE001 - library raises several types
            raise HubError(400, f"Face ID setup failed: {exc}") from exc
        self.db.execute(
            "INSERT OR REPLACE INTO passkeys (id, email, rp_id, public_key, sign_count, label, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                bytes_to_base64url(verified.credential_id), email, self.rp_id(origin),
                verified.credential_public_key, verified.sign_count, label[:40] or "Passkey", int(time.time()),
            ),
        )
        self.db.commit()

    def login_options(self, origin: str) -> dict:
        options = generate_authentication_options(
            rp_id=self.rp_id(origin), user_verification=UserVerificationRequirement.REQUIRED
        )
        key = self._put(Pending("passkey-login", options.challenge, origin, None, time.time() + CHALLENGE_SECONDS))
        return {"challenge_id": key, "options": json.loads(options_to_json(options))}

    def login_finish(self, origin: str, challenge_id: str, credential: dict) -> str:
        pending = self._take(challenge_id, "passkey-login")
        if pending.origin != origin:
            raise HubError(400, "Sign-in started on a different address.")
        row = self.db.execute(
            "SELECT email, public_key, sign_count, rp_id FROM passkeys WHERE id = ?",
            (str(credential.get("id", "")),),
        ).fetchone()
        if row is None:
            raise HubError(401, "This passkey isn't registered with the hub. Sign in with Google instead.")
        email, public_key, sign_count, rp_id = row
        if rp_id != self.rp_id(origin):
            raise HubError(401, "This passkey belongs to a different address.")
        if not self._allowed(email):
            raise HubError(403, f"{email} isn't allowed to use this hub.")
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=pending.value,
                expected_rp_id=rp_id,
                expected_origin=origin,
                credential_public_key=public_key,
                credential_current_sign_count=sign_count,
                require_user_verification=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise HubError(401, f"Face ID sign-in failed: {exc}") from exc
        self.db.execute(
            "UPDATE passkeys SET sign_count = ?, last_used = ? WHERE id = ?",
            (verified.new_sign_count, int(time.time()), credential["id"]),
        )
        self.db.commit()
        return email
