"""Dhan live market feed (DhanHQ v2 websocket) -> MarketState.

Drop-in for feed.LiveFeed. Index tokens are subscribed in Ticker mode, futures
and options in Full mode (LTP, last-trade qty, cumulative volume, OI and 5-level
depth). Every packet is normalized to (token, ts, ltp, cum_volume, oi, bid, ask)
and pushed through MarketState.apply_tick exactly like the Kite feed, so the
footprint / CVD / OI analytics are unchanged - they just get Dhan's ticks.

Binary format (little-endian), from https://dhanhq.co/docs/v2/live-market-feed/
  header 8 bytes : code u8 | msg_len u16 | segment u8 | security_id i32
  1/2 index/ticker : ltp f32 | ltt i32
  4 quote          : ltp f32 | ltq i16 | ltt i32 | atp f32 | vol i32 | tot_sell i32
                     | tot_buy i32 | open f32 | close f32 | high f32 | low f32
  5 OI             : oi i32
  6 prev close     : prev_close f32 | prev_oi i32
  8 full           : ltp f32 | ltq i16 | ltt i32 | atp f32 | vol i32 | tot_sell i32
                     | tot_buy i32 | oi i32 | oi_hi i32 | oi_lo i32 | open f32
                     | close f32 | high f32 | low f32 | 5 x depth(20 bytes)
  depth level      : bid_qty i32 | ask_qty i32 | bid_orders i16 | ask_orders i16
                     | bid_px f32 | ask_px f32
  50 disconnect    : reason i16
"""
from __future__ import annotations

import datetime as dt
import json
import struct
import threading
import time

from .dhan_client import SEG_NAME, dec, enc

WS_URL = "wss://api-feed.dhan.co?version=2&token={tok}&clientId={cid}&authType=2"
REQ_TICKER, REQ_FULL = 15, 21

HDR = struct.Struct("<BHBi")
TICK = struct.Struct("<fi")
QUOTE = struct.Struct("<fhifiiiffff")
FULL = struct.Struct("<fhifiiiiiiffff")
DEPTH = struct.Struct("<iihhff")
SIZE = {1: 16, 2: 16, 4: 8 + QUOTE.size, 5: 12, 6: 16, 7: 8,
        8: 8 + FULL.size + 5 * DEPTH.size, 50: 10}


class DhanFeed:
    def __init__(self, cfg, client, resolved: dict, state):
        self.cfg = cfg
        self.client = client
        self.resolved = resolved
        self.state = state
        self.recorder = None
        self.depth: dict[int, list] = {}        # token -> 5 levels (for depth analytics)
        self.stats = {"packets": 0, "connects": 0, "last_packet": None}
        self._oi: dict[int, float] = {}
        self._stop = threading.Event()
        self._ws = None

    # ------------------------------------------------------------ lifecycle
    def _subscriptions(self):
        idx, full = [], []
        for ri in self.resolved.values():
            for tok in ri.all_tokens():
                seg, sid = dec(tok)
                (idx if seg == "IDX_I" else full).append({"ExchangeSegment": seg,
                                                          "SecurityId": sid})
        return [(REQ_TICKER, idx), (REQ_FULL, full)]

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="dhan-feed").start()

    def stop(self):
        self._stop.set()
        try:
            if self._ws is not None:
                self._ws.send(json.dumps({"RequestCode": 12}))
                self._ws.close()
        except Exception:
            pass

    def _run(self):
        from websockets.sync.client import connect
        url = WS_URL.format(tok=self.client.token, cid=self.client.client_id)
        backoff = 2
        while not self._stop.is_set():
            try:
                with connect(url, max_size=None, open_timeout=20,
                             ping_interval=20, ping_timeout=40) as ws:
                    self._ws = ws
                    self.stats["connects"] += 1
                    n = 0
                    for code, lst in self._subscriptions():
                        for i in range(0, len(lst), 100):
                            chunk = lst[i:i + 100]
                            ws.send(json.dumps({"RequestCode": code,
                                                "InstrumentCount": len(chunk),
                                                "InstrumentList": chunk}))
                            n += len(chunk)
                    print(f"[dhan-feed] subscribed {n} instruments")
                    backoff = 2
                    while not self._stop.is_set():
                        msg = ws.recv(timeout=90)
                        if isinstance(msg, (bytes, bytearray)):
                            self._handle(bytes(msg))
            except TimeoutError:
                print("[dhan-feed] no data for 90s - reconnecting")
            except Exception as e:
                if self._stop.is_set():
                    return
                print(f"[dhan-feed] error: {e} - reconnecting in {backoff}s")
            self._ws = None
            if self._stop.wait(backoff):
                return
            backoff = min(60, backoff * 2)

    # -------------------------------------------------------------- parsing
    def _handle(self, buf: bytes):
        off, n = 0, len(buf)
        while off + HDR.size <= n:
            code, mlen, seg, sid = HDR.unpack_from(buf, off)
            size = SIZE.get(code) or (mlen if mlen >= HDR.size else 0)
            if not size or off + size > n:
                break
            try:
                self._packet(code, seg, sid, buf, off)
            except Exception as e:                                  # never kill the feed
                print(f"[dhan-feed] bad packet code={code}: {e}")
            off += size

    def _packet(self, code, seg, sid, buf, off):
        if seg not in SEG_NAME:
            return
        tok = enc(SEG_NAME[seg], sid)
        p = off + HDR.size
        ts = dt.datetime.now()
        if code in (1, 2):
            ltp, _ltt = TICK.unpack_from(buf, p)
            self._emit(tok, ts, ltp, None, None, None, None)
        elif code == 4:
            ltp, _ltq, _ltt, _atp, vol, *_ = QUOTE.unpack_from(buf, p)
            self._emit(tok, ts, ltp, vol, self._oi.get(tok), None, None)
        elif code == 5:
            (oi,) = struct.unpack_from("<i", buf, p)
            self._oi[tok] = oi
        elif code == 8:
            (ltp, _ltq, _ltt, _atp, vol, _tsq, _tbq, oi,
             _oih, _oil, _o, _c, _h, _l) = FULL.unpack_from(buf, p)
            q = p + FULL.size
            levels = [DEPTH.unpack_from(buf, q + i * DEPTH.size) for i in range(5)]
            self.depth[tok] = levels
            self._oi[tok] = oi
            bid = levels[0][4] or None
            ask = levels[0][5] or None
            self._emit(tok, ts, ltp, vol, oi, bid, ask)
        elif code == 50:
            (reason,) = struct.unpack_from("<h", buf, p)
            print(f"[dhan-feed] server disconnect, reason code {reason}")

    def _emit(self, tok, ts, ltp, vol, oi, bid, ask):
        if not ltp or ltp <= 0:
            return
        self.stats["packets"] += 1
        self.stats["last_packet"] = ts
        self.state.apply_tick(tok, ts, float(ltp), vol, oi, bid=bid, ask=ask)
        if self.recorder is not None:
            try:
                self.recorder.raw_tick(tok, ts, float(ltp), vol, oi, bid=bid, ask=ask)
            except Exception:
                pass
