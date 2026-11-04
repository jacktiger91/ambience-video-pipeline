#!/usr/bin/env python3
"""One-time desktop OAuth and credential checks for the cloud YouTube workflow.

No third-party packages. Secrets are read from protected files or the process
environment and are never printed. The loopback browser flow runs on the user's
local machine; subsequent refreshes and uploads run in the cloud.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
PLAYLIST_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
SECRET_DIR = Path.home() / ".config" / "ambience-youtube"
CLIENT_PATH = SECRET_DIR / "client_secret.json"
TOKEN_PATH = SECRET_DIR / "token.json"


class AuthError(Exception):
    """Safe-to-print authentication failure without response bodies or tokens."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Authorization headers and OAuth POST bodies must stay on the fixed
        # Google endpoints below, even if an intermediary returns a redirect.
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def valid_channel_id(value: str) -> str:
    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", value):
        raise argparse.ArgumentTypeError("channel ID must be UC followed by 22 letters, digits, _ or -")
    return value


def valid_handle(value: str) -> str:
    if not re.fullmatch(r"@?[^/?#\s]{3,30}", value):
        raise argparse.ArgumentTypeError("provide a YouTube handle, for example @QuietHoursRelaxingSounds")
    return "@" + value.lstrip("@")


def _read_json_secret(path: Path | None, variable: str, default: Path) -> dict[str, Any]:
    if path is None and os.environ.get(variable):
        raw = os.environ[variable]
    else:
        source = Path(path or default).expanduser()
        try:
            if os.name == "posix" and source.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise AuthError(f"Protect {source} with chmod 600 before using it.")
            raw = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise AuthError(f"Cannot read protected credential file {source}. See docs/YOUTUBE.md.") from exc
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AuthError(f"Invalid credential JSON in {variable} or its selected file.") from exc
    if not isinstance(value, dict):
        raise AuthError(f"Expected a JSON object for {variable}.")
    return value


def load_client(client_path: Path | str | None = None) -> dict[str, Any]:
    data = _read_json_secret(Path(client_path) if client_path else None, "YOUTUBE_OAUTH_CLIENT_JSON", CLIENT_PATH)
    client = data.get("installed")
    if not isinstance(client, dict):
        raise AuthError("Use the downloaded JSON for a Desktop app OAuth client (with an 'installed' section).")
    if not all(isinstance(client.get(name), str) and client[name] for name in ("client_id", "client_secret")):
        raise AuthError("Desktop OAuth client JSON is missing required fields.")
    return client


def load_credentials(
    client_path: Path | str | None = None, token_path: Path | str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    client = load_client(client_path)
    token = _read_json_secret(Path(token_path) if token_path else None, "YOUTUBE_OAUTH_TOKEN_JSON", TOKEN_PATH)
    if not isinstance(token.get("refresh_token"), str) or not token["refresh_token"]:
        raise AuthError("Refresh token is missing. Run the local authorize command with offline consent.")
    return client, token


def scope_set(token: dict[str, Any]) -> set[str]:
    scopes = token.get("scope", token.get("scopes", []))
    return set(scopes.split()) if isinstance(scopes, str) else set(scopes or [])


def _request_json(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with _OPENER.open(request, timeout=60) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        # Never echo Google's response body: OAuth responses can include secrets.
        if exc.code in (400, 401):
            message = "Google rejected authorization or refresh. Check the client and reauthorize if the token expired or was revoked."
        elif exc.code == 403:
            message = "Google denied access. Check API enablement, granted scopes, test-user access, and network policy."
        else:
            message = "Google authorization request failed; retry after checking service and network access."
        raise AuthError(f"{message} (HTTP {exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AuthError("Cannot reach Google securely through the configured network; verify allowed domains and TLS trust.") from exc
    except (ValueError, UnicodeError) as exc:
        raise AuthError("Google returned an invalid authorization response.") from exc
    if not isinstance(data, dict):
        raise AuthError("Google returned an unexpected authorization response.")
    return data


def _token_request(fields: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(fields).encode("ascii")
    return _request_json(urllib.request.Request(TOKEN_URL, data=data, method="POST",
                                              headers={"Content-Type": "application/x-www-form-urlencoded"}))


def refresh_access_token(client: dict[str, Any], token: dict[str, Any]) -> str:
    response = _token_request({
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "refresh_token": token["refresh_token"], "grant_type": "refresh_token",
    })
    access_token = response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AuthError("Google did not return an access token. Reauthorize the Desktop OAuth client.")
    if "scope" in response:
        token["scope"] = response["scope"]
    return access_token


def verify_channel(access_token: str, expected_channel: str) -> dict[str, Any]:
    try:
        valid_channel_id(expected_channel)
    except argparse.ArgumentTypeError as exc:
        raise AuthError(str(exc)) from exc
    query = urllib.parse.urlencode({"part": "id,snippet,contentDetails", "mine": "true"})
    data = _request_json(urllib.request.Request(
        f"{CHANNELS_URL}?{query}", headers={"Authorization": f"Bearer {access_token}"},
    ))
    channels = data.get("items", [])
    if len(channels) != 1 or channels[0].get("id") != expected_channel:
        actual_ids = [item.get("id", "unknown") for item in channels]
        raise AuthError(
            f"Authenticated channel does not uniquely match {expected_channel}; returned IDs: {actual_ids}. "
            "Reauthorize and select the intended YouTube/Brand Account. No upload was attempted."
        )
    return channels[0]


def resolve_expected_channel(
    access_token: str, expected_channel: str | None = None, expected_handle: str | None = None,
) -> str:
    """Resolve the user's explicit target, independently from the signed-in account."""
    if bool(expected_channel) == bool(expected_handle):
        raise AuthError("Supply exactly one intended channel ID or handle.")
    try:
        if expected_channel:
            return valid_channel_id(expected_channel)
        handle = valid_handle(expected_handle or "")
    except argparse.ArgumentTypeError as exc:
        raise AuthError(str(exc)) from exc
    query = urllib.parse.urlencode({"part": "id", "forHandle": handle})
    data = _request_json(urllib.request.Request(
        f"{CHANNELS_URL}?{query}", headers={"Authorization": f"Bearer {access_token}"},
    ))
    items = data.get("items", [])
    if len(items) != 1:
        raise AuthError(f"YouTube did not resolve the intended handle {handle} to one channel. Check the handle before uploading.")
    try:
        return valid_channel_id(items[0].get("id", ""))
    except argparse.ArgumentTypeError as exc:
        raise AuthError("YouTube returned an invalid channel ID for the intended handle.") from exc


def connection_warning(token: dict[str, Any]) -> str | None:
    status = token.get("oauth_status")
    if status != "production":
        suffix = ""
        if status == "testing":
            try:
                created = datetime.fromisoformat(token["authorized_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                expiry = created + timedelta(days=7)
                suffix = f" Estimated expiry: {expiry.isoformat()}."
            except (ValueError, TypeError, KeyError):
                pass
            return (
                "OAuth app is marked Testing: YouTube-scope refresh tokens normally expire after seven days."
                + suffix + " This connection is suitable for a manual test only, not daily readiness. "
                "Move the OAuth app to Production, then authorize again."
            )
        return "OAuth publishing status is unknown. Confirm Production in Google Auth Platform and authorize again before daily use."
    return None


def _write_token(path: Path, data: dict[str, Any]) -> None:
    path = path.expanduser().absolute()
    checkout = Path(__file__).resolve().parent
    if path.resolve().is_relative_to(checkout):
        raise AuthError("Store OAuth credentials outside the repository, for example ~/.config/ambience-youtube/token.json.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix" and path.parent.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise AuthError("The token directory must have mode 0700. Use a dedicated private directory outside the repository.")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".token-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def authorize(args: argparse.Namespace) -> None:
    client = load_client(args.client_secret)
    requested = {UPLOAD_SCOPE}
    if args.verify_channel or args.manage_playlist:
        requested.add(READONLY_SCOPE)
    if args.manage_playlist:
        requested.add(PLAYLIST_SCOPE)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    result: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass  # The request URL includes the authorization code.

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/oauth2callback" or not secrets.compare_digest(query.get("state", [""])[0], state):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid OAuth callback. Return to the authorization command.")
                return
            if "error" in query:
                result["error"] = "Google sign-in was cancelled or denied."
            elif query.get("code"):
                result["code"] = query["code"][0]
            else:
                result["error"] = "Google did not return an authorization code."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Authorization response received. You may close this tab and return to the terminal.")

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2callback"
        query = urllib.parse.urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect_uri,
            "response_type": "code", "scope": " ".join(sorted(requested)),
            "access_type": "offline", "prompt": "consent select_account",
            "include_granted_scopes": "true", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        print("Opening Google consent in your local browser. Select the intended YouTube/Brand Account.")
        if not webbrowser.open(f"{AUTH_URL}?{query}", new=1):
            raise AuthError("No local browser opened. Run this authorization command on a computer with a browser.")
        deadline = time.monotonic() + 600
        while not result and time.monotonic() < deadline:
            server.handle_request()
        if not result:
            raise AuthError("Google sign-in timed out after ten minutes; run authorize again.")
    if "error" in result:
        raise AuthError(result["error"])
    response = _token_request({
        "code": result["code"], "client_id": client["client_id"], "client_secret": client["client_secret"],
        "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
    })
    refresh_token = response.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise AuthError("Google did not grant offline access. Revoke this app's previous grant and authorize again with consent.")
    token: dict[str, Any] = {
        "refresh_token": refresh_token, "scope": response.get("scope", " ".join(sorted(requested))),
        "oauth_status": args.oauth_status, "authorized_at": datetime.now(timezone.utc).isoformat(),
    }
    if args.expected_channel:
        token["expected_channel_id"] = args.expected_channel
    else:
        token["expected_handle"] = args.expected_handle
    if not requested.issubset(scope_set(token)):
        raise AuthError("Not all requested scopes were granted. Run authorize again and grant the required permissions.")
    if READONLY_SCOPE in requested:
        access_token = response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AuthError("Google did not return an access token for the required channel verification.")
        intended = resolve_expected_channel(access_token, args.expected_channel, args.expected_handle)
        channel = verify_channel(access_token, intended)
        token["expected_channel_id"] = intended
        token["verified_channel_id"] = channel["id"]
        print(f"Verified authenticated channel: {channel['id']}")
    else:
        print("Upload scope granted. Channel verification still needs youtube.readonly; run authorize again with --verify-channel before staging.")
    _write_token(Path(args.token_file), token)
    print(f"Saved refresh token to protected file: {Path(args.token_file).expanduser()}")
    warning = connection_warning(token)
    if warning:
        print(f"Warning: {warning}", file=sys.stderr)


def check(args: argparse.Namespace) -> int:
    client, token = load_credentials(args.client_secret, args.token_file)
    if not {UPLOAD_SCOPE, READONLY_SCOPE}.issubset(scope_set(token)):
        raise AuthError("Staging needs youtube.upload plus youtube.readonly for channel identity, upload recovery, and visibility checks. Authorize with --verify-channel.")
    access_token = refresh_access_token(client, token)
    if not {UPLOAD_SCOPE, READONLY_SCOPE}.issubset(scope_set(token)):
        raise AuthError("The refreshed grant lacks required upload/read-only permissions. Authorize with --verify-channel.")
    intended = resolve_expected_channel(access_token, args.expected_channel, args.expected_handle)
    channel = verify_channel(access_token, intended)
    print(f"Token refresh succeeded; authenticated channel verified: {channel['id']}.")
    warning = connection_warning(token)
    if warning:
        print(f"Warning: {warning}", file=sys.stderr)
        if not args.allow_testing or token.get("oauth_status") != "testing":
            return 2
        print("Manual test permitted. This connection is not ready for unattended daily use.")
    else:
        print("Saved OAuth status is Production. Confirm this matches Google Auth Platform; revocation can still require reauthorization.")
    print("The daily schedule remains disabled.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local browser authorization and cloud refresh/channel verification; never prints credentials.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    auth = subparsers.add_parser("authorize", help="one-time local browser consent (run on your computer, not a headless job)")
    auth.add_argument("--client-secret", type=Path)
    auth.add_argument("--token-file", type=Path, default=TOKEN_PATH)
    auth_target = auth.add_mutually_exclusive_group(required=True)
    auth_target.add_argument("--expected-channel", type=valid_channel_id)
    auth_target.add_argument("--expected-handle", type=valid_handle)
    auth.add_argument("--oauth-status", required=True, choices=("testing", "production"))
    auth.add_argument("--verify-channel", action="store_true", help="also request youtube.readonly for required identity/recovery/visibility checks")
    auth.add_argument("--manage-playlist", action="store_true", help="also request youtube.force-ssl, only needed for automated playlist modification")
    verify = subparsers.add_parser("check", help="refresh from this machine and verify the intended channel")
    verify.add_argument("--client-secret", type=Path)
    verify.add_argument("--token-file", type=Path)
    verify_target = verify.add_mutually_exclusive_group(required=True)
    verify_target.add_argument("--expected-channel", type=valid_channel_id)
    verify_target.add_argument("--expected-handle", type=valid_handle)
    verify.add_argument("--allow-testing", action="store_true", help="permit a manual test while flagging the seven-day expiry; never daily-ready")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "authorize":
            authorize(args)
            return 0
        return check(args)
    except KeyboardInterrupt:
        print("Authorization cancelled.", file=sys.stderr)
        return 130
    except (AuthError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
