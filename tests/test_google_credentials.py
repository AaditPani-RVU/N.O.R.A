"""Tests for finding and vetting the Google OAuth client secrets file.

Both failures these cover cost real setup time and neither said what was wrong.

The file Google hands you is named client_secret_<id>.apps.googleusercontent.com
.json. The code wanted credentials.json and reported "credentials not found"
with the file sitting in the directory it had just searched — and the error
repeated the same name, so there was nothing to work back from.

The second is worse because it fails later: an OAuth client of type "Web
application" parses fine and only breaks at consent, in a browser, as
redirect_uri_mismatch. run_local_server redirects to a localhost port, which a
web client would have to have registered in advance. That is worth catching
before the browser opens.

Stdlib unittest only — run with:  python -m unittest tests.test_google_credentials -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nora.commands.google_services as gs  # noqa: E402

DESKTOP = {"installed": {"client_id": "x", "client_secret": "y",
                         "auth_uri": "https://accounts.google.com/o/oauth2/auth"}}
WEB = {"web": {"client_id": "x", "client_secret": "y",
               "auth_uri": "https://accounts.google.com/o/oauth2/auth"}}


class CredentialDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(gs, "_ROOT", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, name: str, payload: dict) -> Path:
        p = self.tmp / name
        p.write_text(json.dumps(payload))
        return p

    def test_nothing_present_finds_nothing(self):
        self.assertIsNone(gs._find_client_secrets())

    def test_googles_own_filename_is_found(self):
        want = self._write(
            "client_secret_1234-abcd.apps.googleusercontent.com.json", DESKTOP)
        self.assertEqual(gs._find_client_secrets(), want)

    def test_credentials_json_still_wins_when_present(self):
        # An existing setup must not change behaviour.
        self._write("client_secret_1234.apps.googleusercontent.com.json", DESKTOP)
        want = self._write("credentials.json", DESKTOP)
        self.assertEqual(gs._find_client_secrets(), want)

    def test_the_newest_download_wins_among_several(self):
        # Re-downloading after recreating the client is the usual repair, so
        # the newest file is the one that repair produced.
        old = self._write("client_secret_old.apps.googleusercontent.com.json", WEB)
        import os
        os.utime(old, (time.time() - 600, time.time() - 600))
        new = self._write("client_secret_new.apps.googleusercontent.com.json", DESKTOP)
        self.assertEqual(gs._find_client_secrets(), new)


class ClientTypeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(gs, "_ROOT", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        # _TOKEN_FILE is module-level and points at the real root; make sure no
        # real token short-circuits the checks under test.
        token = mock.patch.object(gs, "_TOKEN_FILE", self.tmp / "google_token.json")
        token.start()
        self.addCleanup(token.stop)

    def test_a_web_client_is_refused_before_the_browser_opens(self):
        (self.tmp / "credentials.json").write_text(json.dumps(WEB))
        with self.assertRaises(RuntimeError) as ctx:
            gs._get_creds()
        msg = str(ctx.exception)
        self.assertIn("Web application", msg)
        self.assertIn("Desktop app", msg)

    def test_a_missing_file_names_the_directory_it_looked_in(self):
        with self.assertRaises(RuntimeError) as ctx:
            gs._get_creds()
        msg = str(ctx.exception)
        self.assertIn(str(self.tmp), msg)
        self.assertIn("Desktop app", msg)
        # Must not tell the user to produce a filename Google never gives them.
        self.assertIn("client_secret", msg)

    def test_a_desktop_client_gets_past_the_type_check(self):
        (self.tmp / "credentials.json").write_text(json.dumps(DESKTOP))
        # It should reach the flow rather than raising one of ours.
        with mock.patch("google_auth_oauthlib.flow.InstalledAppFlow"
                        ".from_client_secrets_file") as flow:
            flow.return_value.run_local_server.return_value = mock.MagicMock(
                to_json=lambda: "{}")
            gs._get_creds()
        self.assertTrue(flow.called)


if __name__ == "__main__":
    unittest.main()
