from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from market_data.saxo_oauth import (
    TokenBundle,
    authorization_url,
    load_token_file,
    resolve_access_token,
    save_token_file,
)


class SaxoOAuthTests(unittest.TestCase):
    def test_authorization_url_contains_required_parameters(self):
        url = authorization_url(
            app_key="app-key",
            redirect_uri="http://localhost:8765/callback",
            environment="sim",
            state="state-123",
        )
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "sim.logonvalidation.net")
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["app-key"])
        self.assertEqual(query["state"], ["state-123"])

    def test_valid_token_file_is_reused_without_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.json"
            save_token_file(
                path,
                TokenBundle("access", "refresh", "Bearer", 10_000, 20_000),
            )
            token = resolve_access_token(
                environment="sim",
                token_file=path,
                now=1_000,
            )
            self.assertEqual(token, "access")

    def test_expired_access_token_is_refreshed_and_rotated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.json"
            save_token_file(
                path,
                TokenBundle("old-access", "old-refresh", "Bearer", 900, 2_000),
            )
            calls = []

            def fake_refresher(**kwargs):
                calls.append(kwargs)
                return TokenBundle("new-access", "new-refresh", "Bearer", 2_000, 3_000)

            token = resolve_access_token(
                environment="sim",
                token_file=path,
                app_key="key",
                app_secret="secret",
                now=1_000,
                refresher=fake_refresher,
            )
            self.assertEqual(token, "new-access")
            self.assertEqual(calls[0]["refresh_token"], "old-refresh")
            saved = load_token_file(path)
            self.assertEqual(saved.refresh_token, "new-refresh")


if __name__ == "__main__":
    unittest.main()
