"""Dhan access-token setup, check and keep-alive.

    python -m src.dhan_auth            # paste Client ID + access token (one time)
    python -m src.dhan_auth --check    # verify token, resolve NIFTY/BANKNIFTY, test history
    python -m src.dhan_auth --renew    # extend an ACTIVE token by another 24h

Get the token at web.dhan.co -> My Profile -> "Access DhanHQ APIs" -> generate
Access Token (24h). It is stored only on this Mac in .dhan_token. Renewal
(GET /v2/RenewToken) works only while the token is still valid, so the
keep-alive renews it every few hours outside market hours - if the Mac is off
for >24h, just paste a fresh token again.
"""
from __future__ import annotations

import datetime as dt
import getpass
import json
import sys

import requests

from .config import ROOT

TOKEN_FILE = ROOT / ".dhan_token"
API = "https://api.dhan.co/v2"


def load() -> tuple[str, str]:
    try:
        d = json.loads(TOKEN_FILE.read_text())
        cid, tok = str(d.get("client_id", "")).strip(), str(d.get("access_token", "")).strip()
        if tok:
            return cid, tok
    except Exception:
        pass
    from .config import _dhan_env_fallback       # ~/Documents/dhan/.env
    return _dhan_env_fallback()


def save(client_id: str, token: str, expiry: str | None = None):
    TOKEN_FILE.write_text(json.dumps({
        "client_id": client_id, "access_token": token, "expiry": expiry,
        "saved_at": dt.datetime.now().isoformat(timespec="seconds")}, indent=1))
    try:
        TOKEN_FILE.chmod(0o600)
    except Exception:
        pass
    _sync_shared_env(client_id, token)


def _sync_shared_env(client_id: str, token: str):
    """Keep ~/Documents/dhan/.env (used by the orderflow recorders) on the same token."""
    from pathlib import Path
    p = __import__("os").environ.get("MP_DHAN_DIR") and Path(__import__("os").environ["MP_DHAN_DIR"]) / ".env" or Path.home() / "Documents" / "dhan" / ".env"
    if not p.exists() or not token:
        return
    try:
        lines, hit = p.read_text().splitlines(), False
        for i, ln in enumerate(lines):
            if ln.strip().startswith("DHAN_ACCESS_TOKEN="):
                if ln.split("=", 1)[1].strip().strip('"').strip("'") == token:
                    return                       # already identical
                lines[i] = f"DHAN_ACCESS_TOKEN={token}"; hit = True
        if hit:
            p.write_text("\n".join(lines) + "\n")
    except Exception:
        pass


def _profile(client_id: str, token: str) -> dict:
    r = requests.get(API + "/profile", timeout=20,
                     headers={"access-token": token, "client-id": client_id,
                              "Accept": "application/json"})
    if r.status_code != 200:
        raise SystemExit(f"Token check failed: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def interactive():
    cid0, _ = load()
    print("Dhan login for the Nifty tracker.")
    print("Generate a token at web.dhan.co -> My Profile -> Access DhanHQ APIs.\n")
    cid = input(f"Dhan Client ID{f' [{cid0}]' if cid0 else ''}: ").strip() or cid0
    tok = getpass.getpass("Access token (input hidden): ").strip()
    if not cid or not tok:
        raise SystemExit("Client ID and token are both required.")
    prof = _profile(cid, tok)
    save(cid, tok, prof.get("tokenValidity"))
    print(f"\nSaved. Logged in as client {prof.get('dhanClientId', cid)}"
          f" - token valid until {prof.get('tokenValidity', '?')}")
    print("Data API:", prof.get("dataPlan", "?"), "| valid till", prof.get("dataValidity", "?"))


def renew(quiet: bool = False) -> bool:
    cid, tok = load()
    if not tok:
        if not quiet:
            print("No saved Dhan token - run: python -m src.dhan_auth")
        return False
    r = requests.get(API + "/RenewToken", timeout=20,
                     headers={"access-token": tok, "dhanClientId": cid, "client-id": cid})
    if r.status_code != 200:
        print(f"[dhan] renew failed: HTTP {r.status_code} {r.text[:200]}")
        return False
    try:
        d = r.json()
    except ValueError:
        d = {}
    body = d.get("data", d) if isinstance(d, dict) else {}
    new = next((v for k, v in body.items() if "token" in k.lower() and isinstance(v, str)
                and len(v) > 40), None)
    expiry = next((v for k, v in body.items() if "expir" in k.lower() or "validity" in k.lower()), None)
    save(cid, new or tok, expiry)
    print(f"[dhan] token renewed {dt.datetime.now():%Y-%m-%d %H:%M} -> valid until {expiry or '?'}"
          f"{'' if new else ' (same token string)'}")
    return True


def check():
    from .config import load_config
    from .dhan_client import DhanClient
    from .instruments import resolve_all
    lines = []
    out = lambda s: (print(s), lines.append(s))
    cid, tok = load()
    if not tok:
        raise SystemExit("No saved Dhan token - run: python -m src.dhan_auth")
    prof = _profile(cid, tok)
    out(f"profile: client {prof.get('dhanClientId')} token valid {prof.get('tokenValidity')} "
        f"data plan {prof.get('dataPlan')} till {prof.get('dataValidity')}")
    cfg = load_config()
    cl = DhanClient(cid, tok)
    res = resolve_all(cl, cfg)
    now = dt.datetime.now()
    for k, ri in res.items():
        n_opt = sum(len(v) for v in ri.option_tokens_by_expiry.values())
        out(f"{k}: spot={ri.spot_token} fut={ri.fut_tradingsymbol} ({ri.fut_token}) "
            f"expiries={[str(e) for e in ri.expiries]} option strikes={n_opt}")
        try:
            bars = cl.historical_data(ri.fut_token, now - dt.timedelta(days=4), now,
                                      "15minute", oi=True)
            out(f"   fut 15m bars: {len(bars)}  first={bars[0]['date'] if bars else '-'} "
                f"last={bars[-1]['date'] if bars else '-'} close={bars[-1]['close'] if bars else '-'}")
            sp = cl.historical_data(ri.spot_token, now - dt.timedelta(days=4), now, "15minute")
            out(f"   spot 15m bars: {len(sp)}  last={sp[-1]['date'] if sp else '-'} "
                f"close={sp[-1]['close'] if sp else '-'}")
            d = cl.historical_data(ri.spot_token, now - dt.timedelta(days=10), now, "day")
            out(f"   spot daily bars: {len(d)} last={d[-1]['date'].date() if d else '-'} "
                f"close={d[-1]['close'] if d else '-'}")
        except Exception as e:
            out(f"   history error: {e}")
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "dhan_check.txt").write_text("\n".join(lines) + "\n")
    print(f"\n(saved to {ROOT / 'logs' / 'dhan_check.txt'})")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--renew":
        sys.exit(0 if renew() else 1)
    elif arg == "--check":
        check()
    else:
        interactive()
        print("\nRunning a quick check…\n")
        check()
