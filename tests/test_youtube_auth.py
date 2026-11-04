"""Offline checks for OAuth least privilege, secret handling, and identity checks."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

import youtube_auth as auth


CHANNEL = "UC" + "a" * 22
OTHER = "UC" + "b" * 22
CLIENT = {"installed": {"client_id": "test-client", "client_secret": "client-secret-sentinel"}}


class OAuthTests(unittest.TestCase):
    def test_load_environment_and_refresh_without_secret_output(self):
        token = {"refresh_token": "refresh-sentinel", "scope": auth.UPLOAD_SCOPE}
        output = io.StringIO()
        with patch.dict(os.environ, {"YOUTUBE_OAUTH_CLIENT_JSON": json.dumps(CLIENT), "YOUTUBE_OAUTH_TOKEN_JSON": json.dumps(token)}):
            with patch.object(auth, "_token_request", return_value={"access_token": "access-sentinel"}) as exchange:
                with redirect_stdout(output), redirect_stderr(output):
                    client, grant = auth.load_credentials()
                    self.assertEqual(auth.refresh_access_token(client, grant), "access-sentinel")
                self.assertEqual(exchange.call_args.args[0]["grant_type"], "refresh_token")
                self.assertNotIn("sentinel", output.getvalue())

    def test_broad_file_permissions_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Path(directory) / "client.json"
            client.write_text(json.dumps(CLIENT))
            client.chmod(0o644)
            with self.assertRaisesRegex(auth.AuthError, "chmod 600"):
                auth.load_client(client)
            client.chmod(0o600)
            self.assertEqual(auth.load_client(client)["client_id"], "test-client")

    def test_token_writer_does_not_change_shared_parent_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "shared"
            parent.mkdir(mode=0o755)
            parent.chmod(0o755)
            with self.assertRaisesRegex(auth.AuthError, "dedicated private directory"):
                auth._write_token(parent / "token.json", {"refresh_token": "secret-sentinel"})
            self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
            self.assertFalse((parent / "token.json").exists())

    def test_channel_handle_resolution_is_independent_of_signed_in_channel(self):
        with patch.object(auth, "_request_json", side_effect=[{"items": [{"id": CHANNEL}]}, {"items": [{"id": OTHER}]}]) as request:
            resolved = auth.resolve_expected_channel("access-sentinel", expected_handle="@QuietHoursRelaxingSounds")
            self.assertEqual(resolved, CHANNEL)
            with self.assertRaisesRegex(auth.AuthError, "does not uniquely match"):
                auth.verify_channel("access-sentinel", resolved)
            self.assertIn("forHandle=", request.call_args_list[0].args[0].full_url)
            self.assertIn("mine=true", request.call_args_list[1].args[0].full_url)

    def test_testing_warning_and_check_exit_status(self):
        token = {"refresh_token": "x", "scope": f"{auth.UPLOAD_SCOPE} {auth.READONLY_SCOPE}",
                 "oauth_status": "testing", "authorized_at": datetime.now(timezone.utc).isoformat()}
        args = argparse.Namespace(client_secret=None, token_file=None, expected_channel=CHANNEL,
                                  expected_handle=None, allow_testing=False)
        with patch.object(auth, "load_credentials", return_value=(CLIENT["installed"], token)), \
             patch.object(auth, "refresh_access_token", return_value="access-sentinel"), \
             patch.object(auth, "verify_channel", return_value={"id": CHANNEL}), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(auth.check(args), 2)
            args.allow_testing = True
            self.assertEqual(auth.check(args), 0)
        self.assertIn("seven days", auth.connection_warning(token))
        self.assertIsNone(auth.connection_warning({"oauth_status": "production"}))

    def test_upload_only_grant_cannot_skip_required_verification(self):
        args = argparse.Namespace(client_secret=None, token_file=None, expected_channel=CHANNEL,
                                  expected_handle=None, allow_testing=False)
        with patch.object(auth, "load_credentials", return_value=({}, {"scope": auth.UPLOAD_SCOPE})), \
             patch.object(auth, "refresh_access_token") as refresh:
            with self.assertRaisesRegex(auth.AuthError, "youtube.readonly"):
                auth.check(args)
            refresh.assert_not_called()

    def test_http_failures_do_not_expose_secret_response_bodies(self):
        error = urllib.error.HTTPError(auth.TOKEN_URL, 400, "bad", {}, io.BytesIO(b'{"refresh_token":"secret-sentinel"}'))
        with patch.object(auth._OPENER, "open", side_effect=error):
            with self.assertRaises(auth.AuthError) as caught:
                auth._token_request({"refresh_token": "secret-sentinel"})
            self.assertNotIn("sentinel", str(caught.exception))

    def test_redirects_cannot_forward_credentials_to_another_endpoint(self):
        handler = auth._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://other.invalid/"))

    def test_authorization_uses_pkce_state_offline_and_upload_only_by_default(self):
        opened = {}

        class FakeServer:
            def __init__(self, address, callback):
                self.callback = callback
                self.server_port = 43210
                self.timeout = 1
                self.address = address

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def handle_request(self):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(opened["url"]).query)
                # Invoke the handler with a fake transport; no local socket or Google access.
                receiver = object.__new__(self.callback)
                receiver.path = "/oauth2callback?" + urllib.parse.urlencode({"state": query["state"][0], "code": "code-sentinel"})
                receiver.send_response = lambda *args: None
                receiver.send_header = lambda *args: None
                receiver.end_headers = lambda: None
                receiver.wfile = io.BytesIO()
                receiver.do_GET()

        def open_browser(url, **kwargs):
            opened["url"] = url
            return True

        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "private" / "token.json"
            args = argparse.Namespace(client_secret=None, token_file=token_path, expected_channel=CHANNEL,
                                      expected_handle=None, oauth_status="production", verify_channel=False, manage_playlist=False)
            with patch.object(auth, "load_client", return_value=CLIENT["installed"]), \
                 patch.object(auth, "HTTPServer", FakeServer), \
                 patch.object(auth.webbrowser, "open", side_effect=open_browser), \
                 patch.object(auth, "_token_request", return_value={"refresh_token": "refresh-sentinel", "access_token": "access-sentinel"}) as exchange, \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                auth.authorize(args)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(opened["url"]).query)
            self.assertEqual(query["scope"], [auth.UPLOAD_SCOPE])
            self.assertEqual(query["access_type"], ["offline"])
            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertGreaterEqual(len(query["state"][0]), 32)
            self.assertIn("code_verifier", exchange.call_args.args[0])
            grant = json.loads(token_path.read_text())
            self.assertNotIn("access_token", grant)
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(token_path.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
