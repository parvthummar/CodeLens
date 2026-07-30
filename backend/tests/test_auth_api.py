"""Auth endpoints and the get_current_user dependency."""

import uuid

import pytest

pytestmark = pytest.mark.db

SIGNUP = "/api/v1/auth/signup"
LOGIN = "/api/v1/auth/login"
PROJECTS = "/api/v1/projects/"


def fresh_email() -> str:
    return f"api-{uuid.uuid4().hex[:10]}@example.com"


class TestSignup:
    async def test_creates_a_user(self, client):
        email = fresh_email()
        r = await client.post(SIGNUP, json={"email": email, "password": "pw-12345", "full_name": "A B"})
        assert r.status_code == 201
        body = r.json()
        assert body["email"] == email
        assert body["full_name"] == "A B"
        assert body["is_active"] is True

    async def test_id_is_a_uuid_string(self, client):
        r = await client.post(SIGNUP, json={"email": fresh_email(), "password": "pw-12345"})
        # The frontend treats ids as opaque strings and builds URLs from them.
        assert uuid.UUID(r.json()["id"])

    async def test_password_is_not_echoed(self, client):
        r = await client.post(SIGNUP, json={"email": fresh_email(), "password": "pw-12345"})
        assert "password" not in r.text and "hashed_password" not in r.text

    async def test_duplicate_email_is_rejected(self, client):
        email = fresh_email()
        await client.post(SIGNUP, json={"email": email, "password": "pw-12345"})
        r = await client.post(SIGNUP, json={"email": email, "password": "other-pw"})
        assert r.status_code == 400

    async def test_invalid_email_is_rejected(self, client):
        r = await client.post(SIGNUP, json={"email": "not-an-email", "password": "pw-12345"})
        assert r.status_code == 422

    async def test_missing_password_is_rejected(self, client):
        assert (await client.post(SIGNUP, json={"email": fresh_email()})).status_code == 422


class TestLogin:
    async def test_returns_a_bearer_token(self, client, user):
        r = await client.post(LOGIN, json={"email": user.email, "password": "correct-horse"})
        assert r.status_code == 200
        assert r.json()["access_token"]
        assert r.json()["token_type"] == "bearer"

    async def test_wrong_password_is_unauthorized(self, client, user):
        r = await client.post(LOGIN, json={"email": user.email, "password": "nope"})
        assert r.status_code == 401

    async def test_unknown_email_is_unauthorized(self, client):
        r = await client.post(LOGIN, json={"email": fresh_email(), "password": "pw-12345"})
        assert r.status_code == 401

    async def test_wrong_password_and_unknown_email_are_indistinguishable(self, client, user):
        """Same status and detail either way, so the response cannot enumerate users."""
        bad_pw = await client.post(LOGIN, json={"email": user.email, "password": "nope"})
        no_user = await client.post(LOGIN, json={"email": fresh_email(), "password": "nope"})
        assert bad_pw.status_code == no_user.status_code
        assert bad_pw.json()["detail"] == no_user.json()["detail"]

    async def test_signup_then_login_round_trip(self, client):
        email = fresh_email()
        await client.post(SIGNUP, json={"email": email, "password": "pw-12345"})
        r = await client.post(LOGIN, json={"email": email, "password": "pw-12345"})
        assert r.status_code == 200


class TestAuthenticatedAccess:
    async def test_no_token_is_unauthorized(self, client):
        assert (await client.get(PROJECTS)).status_code == 401

    async def test_garbage_token_is_unauthorized(self, client):
        r = await client.get(PROJECTS, headers={"Authorization": "Bearer nonsense"})
        assert r.status_code == 401

    async def test_malformed_header_is_unauthorized(self, client):
        r = await client.get(PROJECTS, headers={"Authorization": "nonsense"})
        assert r.status_code == 401

    async def test_valid_token_is_accepted(self, client, auth_headers):
        assert (await client.get(PROJECTS, headers=auth_headers)).status_code == 200

    async def test_token_for_a_deleted_user_is_rejected(self, client, db, user):
        """The subject is resolved on every request, not trusted from the token."""
        from app.core.security import create_access_token

        headers = {"Authorization": f"Bearer {create_access_token({'sub': user.email})}"}
        await db.delete(user)
        await db.flush()
        assert (await client.get(PROJECTS, headers=headers)).status_code == 401

    async def test_expired_token_is_rejected(self, client, user):
        from datetime import timedelta

        from app.core.security import create_access_token

        token = create_access_token({"sub": user.email}, expires_delta=timedelta(seconds=-5))
        r = await client.get(PROJECTS, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

    async def test_token_without_subject_is_rejected(self, client):
        from app.core.security import create_access_token

        token = create_access_token({"not_sub": "x"})
        r = await client.get(PROJECTS, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
