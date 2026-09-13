import os
import json
import time
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

MEXC_BASE = "https://api.mexc.com"
SYMBOL = os.getenv("MEXC_SYMBOL", "ETH_USDT")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("ethusdt-bot")


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float

    @property
    def color(self):
        if self.close > self.open:
            return "GREEN"
        if self.close < self.open:
            return "RED"
        return "DOJI"


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_closed_ts": None, "last_start_ts": None,
                "last_start_color": None, "pending": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("Cannot read state; starting fresh")
        return {"last_closed_ts": None, "last_start_ts": None,
                "last_start_color": None, "pending": []}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def mexc_get_1m(limit=300):
    # Public MEXC Futures data; no MEXC API key is required for signals.
    url = f"{MEXC_BASE}/api/v1/contract/kline/{SYMBOL}"
    params = {"interval": "Min1", "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    payload = r.json()
    if not payload.get("success"):
        raise RuntimeError(f"MEXC error: {payload}")
    d = payload["data"]
    rows = []
    for i, ts in enumerate(d["time"]):
        rows.append({
            "ts": int(ts),
            "open": float(d["open"][i]),
            "high": float(d["high"][i]),
            "low": float(d["low"][i]),
            "close": float(d["close"][i]),
        })
    rows.sort(key=lambda x: x["ts"])
    return rows


def aggregate_10m(rows):
    """Build closed UTC 10-minute candles from 1-minute Futures candles."""
    buckets = {}
    for x in rows:
        bucket = (x["ts"] // 600) * 600
        buckets.setdefault(bucket, []).append(x)

    result = []
    now = int(time.time())
    for bucket, items in sorted(buckets.items()):
        if bucket + 600 > now:
            continue
        items.sort(key=lambda x: x["ts"])
        if len(items) < 9:
            continue
        result.append(Candle(
            ts=bucket,
            open=items[0]["open"],
            high=max(x["high"] for x in items),
            low=min(x["low"] for x in items),
            close=items[-1]["close"],
        ))
    return result


def tg_send(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram is not configured. Message would be:\n%s", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    r.raise_for_status()


def fmt_time(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def color_emoji(color):
    return "🟢" if color == "GREEN" else "🔴" if color == "RED" else "⚪"


def process_new_candle(candles, idx, state):
    """
    Rule:
      LAST green/red in a same-color run = START.
      Count 6 candles after START.
      GREEN start + #6 RED => LONG signal.
      RED start + #6 GREEN => SHORT signal.
      LONG: any GREEN on candles #7..#13 = WIN.
      SHORT: any RED on candles #7..#13 = WIN.
      Otherwise LOSS after #13.
    """
    if idx < 1:
        return

    c = candles[idx]
    prev = candles[idx - 1]

    # When color changes, prev is the LAST candle of the previous color run.
    if prev.color in ("GREEN", "RED") and c.color in ("GREEN", "RED") and c.color != prev.color:
        state["last_start_ts"] = prev.ts
        state["last_start_color"] = prev.color

    start_ts = state.get("last_start_ts")
    start_color = state.get("last_start_color")
    if start_ts is None or start_color not in ("GREEN", "RED"):
        return

    start_index = next((j for j in range(idx, -1, -1) if candles[j].ts == start_ts), None)
    if start_index is None:
        return

    offset = idx - start_index

    # Candle #6 after START
    if offset == 6:
        expected_trigger = "RED" if start_color == "GREEN" else "GREEN"
        if c.color == expected_trigger:
            direction = "LONG" if start_color == "GREEN" else "SHORT"
            pending = state.setdefault("pending", [])
            if not any(p["trigger_ts"] == c.ts for p in pending):
                pending.append({
                    "direction": direction,
                    "start_ts": start_ts,
                    "trigger_ts": c.ts,
                    "signal_ts": c.ts,
                })
                tg_send(
                    f"🔔 {SYMBOL} — {direction}\n"
                    f"10m signal\n"
                    f"{color_emoji(start_color)} Start: {fmt_time(start_ts)}\n"
                    f"6-та: {color_emoji(c.color)} {fmt_time(c.ts)}\n"
                    f"Контроль WIN/LOSS: свічки 7–13"
                )
                log.info("SIGNAL %s at %s", direction, fmt_time(c.ts))

    # Check candles #7..#13 for all pending signals.
    pending = state.setdefault("pending", [])
    still_pending = []
    for p in pending:
        rel = (c.ts - p["trigger_ts"]) // 600  # 1 => candle #7, 7 => #13
        if 1 <= rel <= 7:
            win_color = "GREEN" if p["direction"] == "LONG" else "RED"
            if c.color == win_color:
                tg_send(
                    f"✅ WIN — {SYMBOL} {p['direction']}\n"
                    f"Сигнал: {fmt_time(p['signal_ts'])}\n"
                    f"Підтвердження: свічка #{rel + 6}, "
                    f"{color_emoji(c.color)} {fmt_time(c.ts)}"
                )
                log.info("WIN %s trigger=%s", p["direction"], fmt_time(p["trigger_ts"]))
                continue
            if rel == 7:
                tg_send(
                    f"❌ LOSS — {SYMBOL} {p['direction']}\n"
                    f"Сигнал: {fmt_time(p['signal_ts'])}\n"
                    f"У свічках 7–13 не було потрібного кольору."
                )
                log.info("LOSS %s trigger=%s", p["direction"], fmt_time(p["trigger_ts"]))
                continue

        if rel > 7:
            continue
        still_pending.append(p)

    state["pending"] = still_pending


def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

    state = load_state()
    log.info("Started %s 10m signal bot", SYMBOL)

    while True:
        try:
            rows = mexc_get_1m()
            candles = aggregate_10m(rows)
            if not candles:
                time.sleep(POLL_SECONDS)
                continue

            last_ts = state.get("last_closed_ts")
            if last_ts is None:
                # First launch: initialize without sending historical signals.
                state["last_closed_ts"] = candles[-1].ts
                save_state(state)
                log.info("Initialized at %s", fmt_time(candles[-1].ts))
            else:
                new = [c for c in candles if c.ts > last_ts]
                for c in new:
                    idx = candles.index(c)
                    process_new_candle(candles, idx, state)
                    state["last_closed_ts"] = c.ts
                if new:
                    save_state(state)

        except Exception:
            log.exception("Main loop error")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
