# Remote access — run & watch the tracker while you're away

The tracker runs **on this Mac mini**. To reach it from your phone or another
laptop, you set up a small private network (Tailscale) once, then you can open
the dashboard and run commands from anywhere. ~20 min of setup.

---

## ⚠️ Two things that must be true first

1. **The Mac stays powered on and awake.** The app runs here — if the Mac sleeps,
   the tracker stops. Keep it awake (see step 1 below).
2. **The Kite token must be fresh for today.** This app does NOT log you in —
   it reads the token from `~/Documents/NSE/data/kite_token.json`, which your
   existing NSE login refreshes each morning. So just make sure that file shows
   today's date (your usual NSE routine) before starting live. Do NOT run
   `python -m src.kite_auth` — that's not how this setup gets its token.

---

## 1. Keep the Mac awake

System Settings → **Lock Screen** → set *"Turn display off…"* is fine, but under
**Energy / Battery → Options**, enable **"Prevent automatic sleeping when the
display is off."** (Mac mini on power = it can stay up indefinitely.)

Or, when you start the tracker over SSH, wrap it so the Mac can't nap:

```bash
caffeinate -s ~/Documents/nifty-live-tracker/Start-Tracker-LIVE.command
```

---

## 2. Install Tailscale (the private network)

Tailscale is a free, private VPN mesh. Nothing is exposed to the public internet.

1. On the **Mac mini**: download from https://tailscale.com/download → install →
   sign in (Google/GitHub/email). Leave it running.
2. On your **phone/laptop**: install the Tailscale app, sign in with the **same
   account**.
3. On the Mac, find its Tailscale address: open the Tailscale menu-bar icon →
   it shows an IP like `100.x.y.z` (this is the Mac's private address, reachable
   from your other devices anywhere).

---

## 3. View the dashboard from your phone

The dashboard now binds to all interfaces (I set `dashboard.host: 0.0.0.0` in
`config.yaml`), so once Tailscale is on:

- Start the tracker on the Mac (see step 5).
- On your phone/laptop browser, go to:  **http://100.x.y.z:8899**
  (use the Mac's Tailscale IP from step 2.3).

That's the full live dashboard — tickets, chart, footprint, option chain.

---

## 4. Turn on remote commands (SSH)

To run commands (restart the app, edit config, run a backtest):

1. Mac: System Settings → **General → Sharing → Remote Login = ON**.
   (Optionally restrict to your user.)
2. From your laptop terminal, or the **Termius** / **Blink** app on a phone:

   ```bash
   ssh nikhildhawan@100.x.y.z         # the Mac's Tailscale IP
   ```

   Tip: Tailscale also has built-in SSH (`tailscale up --ssh` on the Mac) if you'd
   rather not manage passwords/keys — then `ssh nikhildhawan@<mac-tailscale-name>`
   just works from your Tailscale devices.

3. Optional but nice — **Screen Sharing** for the full Mac GUI (useful for the
   token popup): Mac → Sharing → **Screen Sharing = ON**, then connect from another
   Mac's Finder (`Cmd-K` → `vnc://100.x.y.z`) or a VNC app on your phone.

---

## 5. Daily routine while away (over SSH)

Use the helper scripts — run them ONE at a time (don't paste blocks with comments,
zsh will choke). No need to activate the venv; the scripts do it.

```bash
cd ~/Documents/nifty-live-tracker
bash status.sh        # is anything already running / holding port 8899?
bash start-live.sh    # start LIVE headless (auto-stops 15:35). Token must be fresh.
bash logs.sh          # watch boot/feed logs (Ctrl-C to stop watching)
```

Then open **http://100.x.y.z:8899** on your phone to watch it.

Other commands:

```bash
bash start-sim.sh     # start in SIM (no market data) — good for testing remote access
bash stop.sh          # stop the tracker + free port 8899
nano config.yaml      # tweak settings
# re-run the 2-year backtest:
source .venv/bin/activate && python -m src.setup_backtest --datasets datasets/2024-07-17_to_2026-07-19 --label setup-2yr
```

If `start-live.sh` says the port is still busy, run `bash status.sh` — it shows the
PID and its owner on port 8899. If that PID is yours, `bash stop.sh` clears it; if it
shows another user (e.g. root, from an old auto-start), `sudo kill <pid>` once.

The `--exit-at 15:35` makes LIVE auto-stop after the close so no dead feed runs
overnight; just start it again next morning once the token's fresh.

---

## 6. Security notes

- **Tailscale keeps everything private** — the dashboard/SSH are only reachable by
  *your* logged-in Tailscale devices, not the public internet. This is the safe way.
- Keep the **macOS firewall ON** (System Settings → Network → Firewall) as a belt-
  and-braces measure while `host` is `0.0.0.0`.
- **Avoid** ngrok / public tunnels for this unless you add a password — they put the
  dashboard on the open internet.
- When you're back and don't need remote access, you can set `dashboard.host` back
  to `127.0.0.1` in `config.yaml` to lock the dashboard to this machine again.

---

## 7. Talking to Claude from your phone

You can continue our Cowork session from the **Claude app on your phone or the
web** — it follows you across devices. As long as this Mac's Claude desktop app
stays open and online, Claude can still edit the project files for you. Claude
**cannot** run the live feed itself (it needs this machine's network + your daily
token) — that part stays here on the Mac.
