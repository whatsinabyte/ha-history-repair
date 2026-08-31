"""Complete a fresh Home Assistant's onboarding and mint an access token.

A brand-new instance has no user and no credentials, so nothing can call its
API — including the WebSocket commands the statistics work needs. Home
Assistant exposes the first-run flow over HTTP for exactly this case: create
the owner account, exchange the returned authorisation code for tokens, and
then create a long-lived token that outlives them.

Only ever point this at a throwaway development instance. It creates an owner
account with a password passed on the command line.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Home Assistant's OAuth flow requires a client_id that is a URL it can reach
# back to. For a local instance its own address is the conventional choice.
DEFAULT_CLIENT_ID = "http://localhost:8123/"


def _post(url: str, payload: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    # The URL is a development instance address supplied by the operator on
    # the command line, never untrusted input.
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
        body = response.read().decode()
    return json.loads(body) if body else {}


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    # As above: a developer-supplied local instance URL.
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
        return json.loads(response.read().decode())


def onboard(url: str, username: str, password: str, client_id: str) -> str:
    """Create the owner account and return an authorisation code."""
    result = _post(
        f"{url}/api/onboarding/users",
        {
            "client_id": client_id,
            "name": "Development",
            "username": username,
            "password": password,
            "language": "en",
        },
    )
    code: str = result["auth_code"]
    return code


def exchange(url: str, code: str, client_id: str) -> str:
    tokens = _post_form(
        f"{url}/auth/token",
        {"grant_type": "authorization_code", "code": code, "client_id": client_id},
    )
    access_token: str = tokens["access_token"]
    return access_token


def create_long_lived_token(url: str, access_token: str) -> str:
    """Mint a token that survives the short-lived session token expiring."""
    import websocket  # imported lazily; only this step needs it

    ws = websocket.create_connection(f"{url.replace('http', 'ws')}/api/websocket", timeout=30)
    try:
        json.loads(ws.recv())  # auth_required
        ws.send(json.dumps({"type": "auth", "access_token": access_token}))
        auth_result = json.loads(ws.recv())
        if auth_result.get("type") != "auth_ok":
            raise RuntimeError(f"WebSocket authentication failed: {auth_result}")

        ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "auth/long_lived_access_token",
                    "client_name": f"history-repair-dev-{int(time.time())}",
                    "lifespan": 3650,
                }
            )
        )
        response = json.loads(ws.recv())
        if not response.get("success"):
            raise RuntimeError(f"Could not create a long-lived token: {response}")
        token: str = response["result"]
        return token
    finally:
        ws.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8123")
    parser.add_argument("--username", default="dev")
    parser.add_argument("--password", required=True)
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID)
    parser.add_argument("--out", help="write the token here as well as printing it")
    args = parser.parse_args()

    try:
        code = onboard(args.url, args.username, args.password, args.client_id)
    except urllib.error.HTTPError as err:
        if err.code == 403:
            print(
                "This instance has already been onboarded. Remove its config "
                "directory (./dev/ha_core.sh clean) to start over.",
                file=sys.stderr,
            )
            return 1
        raise

    access_token = exchange(args.url, code, args.client_id)
    token = create_long_lived_token(args.url, access_token)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
