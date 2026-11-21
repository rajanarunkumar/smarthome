"""Sign-in rules: who gets in, CSRF origin checks, and Google token verification."""

import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

ORIGIN = "https://hub.example.ts.net"
CLIENT_ID = "cid.apps.googleusercontent.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from hub import app as appmod

    hub = appmod.Hub()
    hub.secrets.set("auth", {
        "google_client_id": CLIENT_ID,
        "allowed_emails": ["me@example.com"],
        "origins": [ORIGIN],
        "session_hours": 12,
    })
    appmod.app.state.hub = hub
    # No context manager: the lifespan (device discovery) does not run.
    return hub, TestClient(appmod.app, base_url=ORIGIN)


def test_api_requires_sign_in(env):
    _, client = env
    assert client.get("/api/rooms").status_code == 401
    assert client.get("/api/auth/me").json()["signed_in"] is False
    assert client.get("/api/health").status_code == 200


def test_api_token_for_scripts(env):
    hub, client = env
    ok = client.get("/api/rooms", headers={"Authorization": f"Bearer {hub.secrets.access_token()}"})
    assert ok.status_code == 200
    assert client.get("/api/rooms", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_session_cookie_and_origin_check(env):
    hub, client = env
    client.cookies.set("hub_session", hub.auth.create_session("me@example.com", "google"))
    assert client.get("/api/rooms").status_code == 200
    assert client.post("/api/rooms", json={"name": "Den"}).status_code == 403          # no Origin
    assert client.post("/api/rooms", json={"name": "Den"},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/rooms", json={"name": "Den"}, headers={"Origin": ORIGIN}).status_code == 200


def test_session_ends_when_removed_from_allow_list(env):
    hub, client = env
    client.cookies.set("hub_session", hub.auth.create_session("me@example.com", "google"))
    hub.secrets.set("auth", {**hub.auth.config, "allowed_emails": []})
    assert client.get("/api/rooms").status_code == 401


def test_logout(env):
    hub, client = env
    client.cookies.set("hub_session", hub.auth.create_session("me@example.com", "google"))
    assert client.post("/api/auth/logout", headers={"Origin": ORIGIN}).status_code == 200
    client.cookies.set("hub_session", "")
    assert client.get("/api/rooms").status_code == 401


# Google
@pytest.fixture
def google(env, monkeypatch):
    hub, client = env
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class Key:
        key = private.public_key()

    monkeypatch.setattr(hub.auth._jwks, "get_signing_key_from_jwt", lambda token: Key)

    def start():
        url = client.post("/api/auth/google/start", headers={"Origin": ORIGIN}).json()["url"]
        q = parse_qs(urlparse(url).query)
        assert q["client_id"] == [CLIENT_ID] and q["redirect_uri"] == [ORIGIN + "/"]
        return q["state"][0], q["nonce"][0]

    def token(nonce, email="me@example.com", aud=CLIENT_ID, verified=True):
        now = int(time.time())
        claims = {"iss": "https://accounts.google.com", "aud": aud, "sub": "1", "email": email,
                  "email_verified": verified, "nonce": nonce, "iat": now, "exp": now + 600}
        return jwt.encode(claims, private, algorithm="RS256")

    return client, start, token


def finish(client, id_token, state):
    return client.post("/api/auth/google", json={"id_token": id_token, "state": state}, headers={"Origin": ORIGIN})


def test_google_sign_in_allowed(google):
    client, start, token = google
    state, nonce = start()
    resp = finish(client, token(nonce), state)
    assert resp.status_code == 200 and resp.json()["email"] == "me@example.com"
    cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert client.get("/api/rooms").status_code == 200


def test_google_rejects_other_accounts_and_bad_tokens(google):
    client, start, token = google
    state, nonce = start()
    assert finish(client, token(nonce, email="stranger@example.com"), state).status_code == 403
    state, nonce = start()
    assert finish(client, token("wrong-nonce"), state).status_code == 401
    state, nonce = start()
    assert finish(client, token(nonce, aud="someone-else"), state).status_code == 401
    state, nonce = start()
    assert finish(client, token(nonce, verified=False), state).status_code == 403


def test_google_state_is_single_use(google):
    client, start, token = google
    state, nonce = start()
    assert finish(client, token(nonce), state).status_code == 200
    assert finish(client, token(nonce), state).status_code == 400


def test_google_start_needs_allowed_origin(google):
    client, _, _ = google
    assert client.post("/api/auth/google/start", headers={"Origin": "https://evil.example"}).status_code == 403


# Passkeys
def test_passkey_registration_needs_a_signed_in_user(env):
    hub, client = env
    assert client.post("/api/auth/passkey/register/options", headers={"Origin": ORIGIN}).status_code == 401
    client.cookies.set("hub_session", hub.auth.create_session("me@example.com", "google"))
    opts = client.post("/api/auth/passkey/register/options", headers={"Origin": ORIGIN}).json()["options"]
    assert opts["rp"]["id"] == "hub.example.ts.net"
    assert opts["authenticatorSelection"]["userVerification"] == "required"


def test_passkey_login_options_are_public(env):
    _, client = env
    resp = client.post("/api/auth/passkey/login/options", headers={"Origin": ORIGIN})
    assert resp.status_code == 200 and resp.json()["options"]["rpId"] == "hub.example.ts.net"
