"""Own auth: argon2id, access JWT, refresh rotation with reuse detection per family."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")  # the repository-wide CI job does not install the API

import jwt  # noqa: E402
from apiservice_fixtures import *  # noqa: E402,F401,F403  (fixtures)
from apiservice_helpers import hdr, sign_up  # noqa: E402

from api import auth  # noqa: E402


def test_sign_up_stores_argon2id_with_the_contract_parameters(client, owner_db):
    me = sign_up(client, "Owner@Example.test")
    assert me["token_type"] == "Bearer" and len(me["refresh_token"]) == 43
    row = owner_db.execute("SELECT password_hash FROM biz.users WHERE id = %s", (me["user"]["id"],)).fetchone()
    assert row["password_hash"].startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert me["password"] not in row["password_hash"]
    sess = owner_db.execute("SELECT refresh_token_hash FROM biz.sessions WHERE user_id = %s", (me["user"]["id"],)).fetchone()
    assert bytes(sess["refresh_token_hash"]) == auth.crypto.sha256(me["refresh_token"])  # only the hash is stored

    r = client.get("/v1/me", headers=hdr(me))
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == "Owner@Example.test"
    assert [m["role"] for m in body["memberships"]] == ["owner"]
    assert body["memberships"][0]["workspace"]["id"] == me["workspace_id"]


def test_duplicate_email_is_409_case_insensitive(client):
    sign_up(client, "dup@example.test")
    r = client.post("/v1/auth/sign-up", json={"email": "DUP@example.test", "password": "x" * 12, "workspace_name": "W"})
    assert r.status_code == 409 and r.json()["code"] == "email_taken"
    assert r.headers["content-type"].startswith("application/problem+json")


def test_short_password_is_validation_failed(client):
    r = client.post("/v1/auth/sign-up", json={"email": "a@example.test", "password": "short", "workspace_name": "W"})
    assert r.status_code == 422
    assert r.json()["code"] == "validation_failed"
    assert any(e["field"] == "/password" for e in r.json()["errors"])


def test_sign_in_wrong_email_and_wrong_password_look_the_same(client):
    me = sign_up(client)
    a = client.post("/v1/auth/sign-in", json={"email": "nobody@example.test", "password": "whatever-password"})
    b = client.post("/v1/auth/sign-in", json={"email": me["email"], "password": "wrong-password!!"})
    assert a.status_code == b.status_code == 401
    assert a.json()["code"] == b.json()["code"] == "invalid_credentials"
    ok = client.post("/v1/auth/sign-in", json={"email": me["email"].upper(), "password": me["password"]})
    assert ok.status_code == 200
    assert ok.json()["workspace_id"] == me["workspace_id"]


def test_ten_failures_lock_the_account(client):
    me = sign_up(client)
    for _ in range(10):
        r = client.post("/v1/auth/sign-in", json={"email": me["email"], "password": "wrong-password!!"})
        assert r.json()["code"] == "invalid_credentials"
    client.rp.limiter.reset()
    r = client.post("/v1/auth/sign-in", json={"email": me["email"], "password": me["password"]})
    assert r.status_code == 401 and r.json()["code"] == "account_locked"


def test_sign_in_is_rate_limited_per_account(client):
    me = sign_up(client)
    codes = [client.post("/v1/auth/sign-in", json={"email": me["email"], "password": "wrong-password!!"}).status_code
             for _ in range(11)]  # fmt: skip
    assert codes[-1] == 429


def test_refresh_rotates_and_reuse_revokes_the_family(client, owner_db):
    me = sign_up(client)
    first = me["refresh_token"]
    r1 = client.post("/v1/auth/refresh", json={"refresh_token": first})
    assert r1.status_code == 200
    second = r1.json()["refresh_token"]
    assert second != first
    assert client.get("/v1/me", headers=hdr(r1.json()["access_token"])).status_code == 200

    # A second sign-in is another family and must survive.
    other = client.post("/v1/auth/sign-in", json={"email": me["email"], "password": me["password"]}).json()

    replay = client.post("/v1/auth/refresh", json={"refresh_token": first})
    assert replay.status_code == 401 and replay.json()["code"] == "refresh_reused"
    # The whole family is revoked: the newest token and its access tokens stop working.
    assert client.post("/v1/auth/refresh", json={"refresh_token": second}).status_code == 401
    assert client.get("/v1/me", headers=hdr(r1.json()["access_token"])).status_code == 401
    assert client.get("/v1/me", headers=hdr(me)).status_code == 401
    reasons = {r["revoked_reason"] for r in owner_db.execute(
        "SELECT revoked_reason FROM biz.sessions WHERE user_id = %s AND family_id = (SELECT family_id FROM biz.sessions"
        " WHERE refresh_token_hash = %s)", (me["user"]["id"], auth.crypto.sha256(first))).fetchall()}  # fmt: skip
    assert reasons == {"reuse_detected"}
    # The other family is untouched.
    assert client.post("/v1/auth/refresh", json={"refresh_token": other["refresh_token"]}).status_code == 200


def test_unknown_and_expired_refresh_tokens(client, owner_db):
    assert client.post("/v1/auth/refresh", json={"refresh_token": "nope"}).json()["code"] == "unauthenticated"
    me = sign_up(client)
    owner_db.execute(
        "UPDATE biz.sessions SET created_at = now() - interval '40 days', expires_at = now() - interval '10 days'"
        " WHERE user_id = %s",
        (me["user"]["id"],),
    )
    r = client.post("/v1/auth/refresh", json={"refresh_token": me["refresh_token"]})
    assert r.status_code == 401 and r.json()["code"] == "token_expired"


def test_sign_out_revokes_the_family_and_is_204_for_unknown_tokens(client):
    me = sign_up(client)
    assert client.post("/v1/auth/sign-out", json={"refresh_token": "unknown"}).status_code == 204
    assert client.post("/v1/auth/sign-out", json={"refresh_token": me["refresh_token"]}).status_code == 204
    assert client.get("/v1/me", headers=hdr(me)).status_code == 401
    assert client.post("/v1/auth/refresh", json={"refresh_token": me["refresh_token"]}).status_code == 401


def test_access_token_checks(client, settings):
    me = sign_up(client)
    assert client.get("/v1/me").json()["code"] == "unauthenticated"
    claims = jwt.decode(me["access_token"], settings.jwt_signing_key, algorithms=["HS256"], audience="riskpredict-web")
    assert claims["iss"] == "riskpredict-api" and claims["sub"] == me["user"]["id"]
    assert claims["exp"] - claims["iat"] == 15 * 60
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    expired, _ = auth.issue_access(settings.jwt_signing_key, me["user"]["id"], claims["sid"], past)
    r = client.get("/v1/me", headers=hdr(expired))
    assert r.status_code == 401 and r.json()["code"] == "token_expired"
    forged = jwt.encode({**claims, "exp": claims["exp"] + 3600}, b"x" * 32, algorithm="HS256")
    assert client.get("/v1/me", headers=hdr(forged)).status_code == 401
    none_alg = jwt.encode(claims, None, algorithm="none")
    assert client.get("/v1/me", headers=hdr(none_alg)).status_code == 401


def test_password_change_revokes_other_sessions(client):
    me = sign_up(client)
    other = client.post("/v1/auth/sign-in", json={"email": me["email"], "password": me["password"]}).json()
    bad = client.patch("/v1/me", headers=hdr(me), json={"current_password": "wrong", "new_password": "a new password!"})
    assert bad.status_code == 422
    r = client.patch("/v1/me", headers=hdr(me), json={"current_password": me["password"], "new_password": "a new password!",
                                                      "display_name": "Ada"})  # fmt: skip
    assert r.status_code == 200 and r.json()["display_name"] == "Ada"
    assert client.get("/v1/me", headers=hdr(me)).status_code == 200
    assert client.get("/v1/me", headers=hdr(other)).status_code == 401
    assert client.post("/v1/auth/sign-in", json={"email": me["email"], "password": "a new password!"}).status_code == 200
