"""Zerodha Kite Connect authentication.

Kite access tokens expire daily (~6 AM IST next day). Run this module once each
morning to refresh the token:

    python -m src.kite_auth

It prints the login URL, you approve in the browser, paste back the
`request_token` from the redirect URL, and it stores a fresh access token in
`.kite_token`.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .config import ROOT, load_config


def build_kite(cfg):
    """Return an authenticated KiteConnect client (or None in sim mode)."""
    try:
        from kiteconnect import KiteConnect
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            "kiteconnect not installed. Run: pip install -r requirements.txt"
        ) from e

    kite = KiteConnect(api_key=cfg.api_key)
    if cfg.access_token:
        kite.set_access_token(cfg.access_token)
    return kite


def interactive_login() -> None:
    cfg = load_config()
    if not cfg.api_key or not cfg.api_secret:
        raise SystemExit("Set KITE_API_KEY and KITE_API_SECRET in .env first.")

    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=cfg.api_key)
    print("\n1) Open this URL, log in, and approve:\n")
    print("   " + kite.login_url())
    print(
        "\n2) You'll be redirected to your app's redirect URL. Copy the "
        "`request_token=...` value from that URL.\n"
    )
    request_token = input("Paste request_token here: ").strip()

    data = kite.generate_session(request_token, api_secret=cfg.api_secret)
    access_token = data["access_token"]
    (ROOT / ".kite_token").write_text(access_token)
    print(f"\nSaved access token to {ROOT / '.kite_token'}")
    print(f"Logged in as: {data.get('user_name', data.get('user_id'))}")


if __name__ == "__main__":
    interactive_login()
