#!/usr/bin/env python3
"""Perform the one-time Saxo OAuth login on localhost and persist tokens safely."""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from market_data.saxo_oauth import (
    DEFAULT_REDIRECT_URI,
    DEFAULT_TOKEN_FILE,
    authorization_url,
    exchange_authorization_code,
    make_state,
    save_token_file,
)


class CallbackState:
    code: str | None = None
    error: str | None = None
    returned_state: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", default=os.getenv("SAXO_ENV", "sim"))
    parser.add_argument(
        "--redirect-uri", default=os.getenv("SAXO_REDIRECT_URI", DEFAULT_REDIRECT_URI)
    )
    parser.add_argument(
        "--token-file", type=Path, default=Path(os.getenv("SAXO_TOKEN_FILE", DEFAULT_TOKEN_FILE))
    )
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app_key = os.getenv("SAXO_APP_KEY")
    app_secret = os.getenv("SAXO_APP_SECRET")
    if not app_key or not app_secret:
        raise SystemExit("Set SAXO_APP_KEY and SAXO_APP_SECRET before running this script")

    parsed = urlparse(args.redirect_uri)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise SystemExit("The login helper only accepts a localhost HTTP redirect URI")
    port = parsed.port or 80
    expected_path = parsed.path or "/"
    expected_state = make_state()
    callback = CallbackState()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            request = urlparse(self.path)
            if request.path != expected_path:
                self.send_response(404)
                self.end_headers()
                return
            query = parse_qs(request.query)
            callback.code = query.get("code", [None])[0]
            callback.error = query.get("error", [None])[0]
            callback.returned_state = query.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"Saxo authorization received. You can close this tab and return to the terminal."
            )
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *_args) -> None:
            return

    url = authorization_url(
        app_key=app_key,
        redirect_uri=args.redirect_uri,
        environment=args.environment,
        state=expected_state,
    )
    print("Open this Saxo authorization page in your browser:")
    print(url)
    if not args.no_browser:
        webbrowser.open(url)

    server = HTTPServer((parsed.hostname, port), Handler)
    print(f"Waiting for the callback on {args.redirect_uri} ...")
    server.serve_forever()

    if callback.error:
        raise SystemExit(f"Saxo authorization failed: {callback.error}")
    if callback.returned_state != expected_state:
        raise SystemExit("Saxo authorization state mismatch")
    if not callback.code:
        raise SystemExit("Saxo callback did not contain an authorization code")

    bundle = exchange_authorization_code(
        app_key=app_key,
        app_secret=app_secret,
        code=callback.code,
        redirect_uri=args.redirect_uri,
        environment=args.environment,
    )
    save_token_file(args.token_file, bundle)
    print(f"OAuth tokens saved securely to {args.token_file}")
    print("No token or secret was printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
