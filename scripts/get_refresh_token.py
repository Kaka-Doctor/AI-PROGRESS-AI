#!/usr/bin/env python3
"""One-time helper: authorize the agent to upload to YOUR YouTube channel.

You run this ONCE on your own computer (or anywhere with a browser). It opens
your browser, you sign in with the Google account that owns the channel, and
it prints a refresh token. You never share your password with anyone — not
even this script.

Requirements: Python 3.8+ (no pip installs needed).

Usage:
    python3 scripts/get_refresh_token.py --client-id XXX --client-secret YYY

    # or pick values up from the environment:
    YT_CLIENT_ID=xxx YT_CLIENT_SECRET=yyy python3 scripts/get_refresh_token.py

    # no local server possible (phone / remote box)? manual paste mode:
    python3 scripts/get_refresh_token.py --manual

OAuth client setup (Google Cloud Console → APIs & Services → Credentials
→ Create credentials → OAuth client ID):
  - Application type **Desktop app** works as-is, OR
  - Application type **Web application**: add this EXACT Authorized redirect
    URI first:  http://localhost:8765
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def build_auth_url(client_id: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str, client_id: str, client_secret: str) -> dict:
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


class Handler(BaseHTTPRequestHandler):
    code: str | None = None
    error: str | None = None

    def do_GET(self):  # noqa: N802
        query = urllib.parse.urlparse(self.path).query
        params = dict(urllib.parse.parse_qsl(query))
        if "code" in params:
            Handler.code = params["code"]
        else:
            Handler.error = params.get("error", "unknown error")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;text-align:center;"
            b"padding-top:60px'><h2>&#10003; Authorization received</h2>"
            b"<p>You can close this tab and go back to your terminal.</p>"
            b"</body></html>"
        )

    def log_message(self, *args):  # silence
        pass


def main() -> int:
    import os
    ap = argparse.ArgumentParser(description="Get a YouTube refresh token (one time).")
    ap.add_argument("--client-id", default=os.environ.get("YT_CLIENT_ID", ""),
                    help="OAuth client ID (or env YT_CLIENT_ID)")
    ap.add_argument("--client-secret", default=os.environ.get("YT_CLIENT_SECRET", ""),
                    help="OAuth client secret (or env YT_CLIENT_SECRET)")
    ap.add_argument("--manual", action="store_true",
                    help="no local server: authorize in any browser, then paste "
                         "the full redirected URL (or just the code) here")
    args = ap.parse_args()

    client_id = args.client_id or input("Paste your OAuth Client ID: ").strip()
    client_secret = args.client_secret or input(
        "Paste your OAuth Client Secret: ").strip()

    url = build_auth_url(client_id)

    if args.manual:
        print("\n1. Open this link in a browser and sign in with the Google")
        print("   account that owns your YouTube channel, then click Allow:\n")
        print(f"  {url}\n")
        print("2. The browser will land on an address like")
        print(f"   {REDIRECT_URI}/?code=4/0A...  (an error page is NORMAL —")
        print("   nothing is listening there; the code is in the address bar)")
        raw = input("\n3. Paste that FULL address (or just the code value): ").strip()
        code = raw
        if "code=" in raw:
            qs = raw.split("?", 1)[1].split("#", 1)[0]
            params = dict(urllib.parse.parse_qsl(qs))
            code = params.get("code", raw)
        code = code.strip().strip('"').strip("'")
        error = None
    else:
        print("\nOpening your browser to sign in with Google…")
        print("If it does not open, copy this link into your browser:\n")
        print(f"  {url}\n")
        print("Sign in with the Google account that owns your YouTube channel,")
        print("then click 'Allow' on the consent screen.\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        server = HTTPServer(("127.0.0.1", PORT), Handler)
        server.handle_request()
        code, error = Handler.code, Handler.error

    if error or not code:
        print(f"Authorization failed: {error or 'no code received'}")
        return 1

    try:
        data = exchange_code(code, client_id, client_secret)
    except urllib.error.HTTPError as exc:
        print(f"Token exchange failed (HTTP {exc.code}): "
              f"{exc.read().decode()[:400]}")
        return 1

    refresh_token = data.get("refresh_token")
    if not refresh_token:
        print("No refresh token returned. Re-run this script and make sure "
              "you see the consent screen (prompt=consent forces it).")
        print(json.dumps(data, indent=2))
        return 1

    with open("refresh_token.txt", "w", encoding="utf-8") as fh:
        fh.write(refresh_token + "\n")

    print("\n" + "=" * 64)
    print("SUCCESS! Your refresh token (also saved to refresh_token.txt):")
    print("=" * 64)
    print(refresh_token)
    print("=" * 64)
    print("\nNext steps:")
    print("  1. Go to your GitHub repo → Settings → Secrets and variables")
    print("     → Actions → New repository secret")
    print("  2. Name: YT_REFRESH_TOKEN   Value: the token above")
    print("  3. Also add YT_CLIENT_ID and YT_CLIENT_SECRET the same way.")
    print("\nThat's it — the agent can now upload to your channel forever.")
    print("(If uploads ever stop working, just re-run this script and")
    print(" update the YT_REFRESH_TOKEN secret with the new token.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
