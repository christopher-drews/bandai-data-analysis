"""Auth-managing HTTP session for the LootVault upload scripts.

Long runs (e.g. the per-month sales lifecycle) can outlive a single JWT. This
provides an ``AuthSession`` (a ``requests.Session`` subclass) that:

  * injects ``Authorization: Bearer <token>`` on every request, and
  * on a ``401``, re-authenticates with stored credentials and retries once —
    so an expiring token is refreshed transparently mid-run.

Because it overrides ``request``, existing call sites (``session.get``,
``session.post``, and the shared ``post_with_retry``) get refresh for free with
no changes. Pass a static ``--token`` and it behaves exactly as before (no creds
→ no refresh). Pass ``--email``/``--password`` and it self-refreshes.

Login contract (same as the lootvault CLI): POST /api/identity/v1/user/login
with ``{email, password}`` -> ``{accessToken}``.
"""

from __future__ import annotations

import sys

import requests

LOGIN_PATH = "/api/identity/v1/user/login"
LOGIN_TIMEOUT_S = 30


def _base_url(host: str) -> str:
    host = host.strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    return host


class AuthSession(requests.Session):
    """requests.Session that adds a bearer token and refreshes it on 401."""

    def __init__(self, host: str, *, token: str | None = None,
                 email: str | None = None, password: str | None = None) -> None:
        super().__init__()
        self._base = _base_url(host)
        self._email = email
        self._password = password
        self._token = token
        if self._token is None:
            self._login()

    def _login(self) -> None:
        if not (self._email and self._password):
            raise SystemExit("Token expired and no --email/--password to re-authenticate.")
        # Plain requests.post (not self) so login itself never recurses through auth.
        resp = requests.post(
            f"{self._base}{LOGIN_PATH}",
            json={"email": self._email, "password": self._password},
            timeout=LOGIN_TIMEOUT_S,
        )
        if not resp.ok:
            raise SystemExit(f"Login failed ({resp.status_code}): {resp.text[:300]}")
        data = resp.json()
        # The server returns snake_case `access_token` (matches the lootvault CLI's
        # LoginResponse); accept camelCase too, just in case.
        token = data.get("access_token") or data.get("accessToken")
        if not token:
            raise SystemExit(f"Login response had no access_token: {str(data)[:200]}")
        self._token = token
        print("  (re)authenticated", file=sys.stderr)

    def request(self, method, url, **kwargs):  # type: ignore[override]
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = f"Bearer {self._token}"
        resp = super().request(method, url, headers=headers, **kwargs)
        # Refresh once on an expired/invalid token, then retry the same request.
        if resp.status_code == 401 and self._email and self._password:
            self._login()
            headers["Authorization"] = f"Bearer {self._token}"
            resp = super().request(method, url, headers=headers, **kwargs)
        return resp


def build_session(host: str, token: str | None, email: str | None, password: str | None) -> AuthSession:
    """Build an AuthSession from a static token OR email+password (one required)."""
    if not token and not (email and password):
        raise SystemExit("Provide --token, or both --email and --password.")
    return AuthSession(host, token=token, email=email, password=password)
