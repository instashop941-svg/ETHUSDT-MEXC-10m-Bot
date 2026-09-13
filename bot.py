import os
import json
import time
import threading
import logging
from collections import OrderedDict
from datetime import datetime, timezone

import requests
import websocket
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip()
SYMBOL = os.getenv('MEXC_SYMBOL', 'ETH_USDT').strip().upper()
POLL_SECONDS = int(os.getenv('POLL_SECONDS', '15'))
STATE_FILE = os.getenv('STATE_FILE', 'state.json')
LOG_EVERY_SECONDS = int(os.getenv('LOG_EVERY_SECONDS', '60'))

REST_URL = f'https://api.mexc.com/api/v1/contract/kline/{SYMBOL}'
WS_URL = 'wss://contract.mexc.com/edge'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
)
log = logging.getLogger('eth-bot')


def utc_dt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def color(c):
    o, cl = float(c['open']), float(c['close'])
    if cl > o:
        return 'GREEN'
    if cl < o:
        return 'RED'
    return 'DOJI'


def load_state():
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'last_processed_10m': None, 'pending': []}


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def telegram_send(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error('TELEGRAM NOT CONFIGURED')
        return False
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    try:
        r = requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=15)
        if r.ok and r.json().get('ok'):
            log.info('TELEGRAM SENT OK')
            return True
        log.error('TELEGRAM ERROR status=%s body=%s', r.status_code, r.text[:500])
    except Exception as e:
        log.exception('TELEGRAM EXCEPTION: %s', e)
    return False


def fetch_rest_seed():
    headers = {
        'Cache-Control': 'no-cache, no-store, max-age=0',
        'Pragma': 'no-cache',
        'User-Agent': 'ETHUSDT-10m-Signal-Bot/2.0',
        'Connection': 'close',
    }
    params = {'interval': 'Min1', 'limit': 300, '_ts': str(int(time.time() * 1000))}
    r = requests.get(REST_URL, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if not payload.get('success', True):
        raise RuntimeError(f'MEXC REST error: {payload}')
    data = payload.get('data')
    if not data:
        raise RuntimeError('MEXC REST returned empty data')
    return data


def parse_rest(data):
    # Current MEXC futures REST commonly returns arrays: [time, open, close, high, low, vol, amount]
    candles = []
    for row in data:
        if isinstance(row, dict):
            ts = int(row.get('time') or row.get('t'))
            o = float(row.get('open') or row.get('o'))
            cl = float(row.get('close') or row.get('c'))
        else:
            ts = int(row[0])
            o = float(row[1])
            cl = float(row[2])
        candles.append({'ts': ts, 'open': o, 'close': cl, 'source': 'REST'})
    candles.sort(key=lambda x: x['ts'])
    return candles


def bucket_10m(minute_ts):
    return (minute_ts // 600) * 600


def aggregate_10m(minute_candles):
    buckets = OrderedDict()
    for c in sorted(minute_candles, key=lambda x: x['ts']):
        b = bucket_10m(c['ts'])
        item = buckets.get(b)
        if item is None:
            item = {'ts': b, 'open': c['open'], 'close': c['close'], 'count': 0}
            buckets[b] = item
        item['close'] = c['close']
        item['count'] += 1
    # only complete 10m buckets with >=9 minutes
    now = int(time.time())
    out = []
    for b, c in buckets.items():
        if b + 600 <= now and c['count'] >= 9:
            out.append(c)
    return out


class SignalEngine:
    def __init__(self, state):
        self.state = state
        self.candles = OrderedDict()
        self.pending = []
        self.last_logged = 0
        self.seeded = False

    def seed(self, candles):
        agg = aggregate_10m(candles)
        for c in agg:
            self.candles[c['ts']] = c
        self.candles = OrderedDict(sorted(self.candles.items()))
        # Keep enough history for strategy evaluation.
        while len(self.candles) > 120:
            self.candles.popitem(last=False)
        latest = next(reversed(self.candles.values())) if self.candles else None
        if latest:
            self.state['last_processed_10m'] = latest['ts']
            save_state(self.state)
            log.info('SEEDED | closed_10m=%d | latest=%s %s | O=%.4f C=%.4f',
                     len(self.candles), utc_dt(latest['ts']), color(latest), latest['open'], latest['close'])
        self.seeded = True

    def on_closed_10m(self, candle):
        ts = candle['ts']
        if ts in self.candles:
            return
        self.candles[ts] = candle
        self.candles = OrderedDict(sorted(self.candles.items()))
        while len(self.candles) > 120:
            self.candles.popitem(last=False)
        log.info('NEW 10M | %s %s | O=%.4f C=%.4f', utc_dt(ts), color(candle), candle['open'], candle['close'])
        self.evaluate_new_candle(ts)
        self.state['last_processed_10m'] = ts
        save_state(self.state)

    def evaluate_new_candle(self, ts):
        keys = list(self.candles.keys())
        if ts not in self.candles:
            return
        idx = keys.index(ts)

        # Check existing pending signals. rel=1..7 correspond to candles 7..13.
        still_pending = []
        current = self.candles[ts]
        for p in self.pending:
            rel = idx - p['start_idx']
            if rel < 1:
                still_pending.append(p)
                continue
            expected_win = 'GREEN' if p['direction'] == 'LONG' else 'RED'
            if color(current) == expected_win:
                log.info('RESULT WIN | %s | start=%s | control=%d', p['direction'], utc_dt(p['start_ts']), rel)
                telegram_send(f'â WIN\nETHUSDT Futures\nDirection: {p["direction"]}\nControl candle: {rel}/7')
            elif rel >= 7:
                log.info('RESULT LOSS | %s | start=%s | no matching control candle in 7..13', p['direction'], utc_dt(p['start_ts']))
                telegram_send(f'â LOSS\nETHUSDT Futures\nDirection: {p["direction"]}\nNo confirmation in candles 7â13')
            else:
                still_pending.append(p)
        self.pending = still_pending

        # New signal candidate: previous same-color run ends at the start candle.
        # We only evaluate when enough candles exist and the current candle is the 6th after start.
        if idx < 7:
            return
        start_idx = idx - 6
        start = self.candles[keys[start_idx]]
        after = [self.candles[keys[start_idx + j]] for j in range(1, 7)]
        if len(after) < 6:
            return

        start_color = color(start)
        if start_color not in ('GREEN', 'RED'):
            return
        # Start must be the last candle of a same-color run: immediately after it changes color.
        if start_idx + 1 < len(keys):
            # At current processing point, start+1..6 are known. A start is valid only if
            # start+1 is opposite color (the first subsequent candle begins the new run).
            if color(after[0]) == start_color:
                return
        sixth_color = color(after[-1])
        if start_color == 'GREEN' and sixth_color == 'RED':
            self.create_signal(start, idx, 'LONG')
        elif start_color == 'RED' and sixth_color == 'GREEN':
            self.create_signal(start, idx, 'SHORT')

    def create_signal(self, start, current_idx, direction):
        # Prevent duplicate signal for same start.
        if any(p['start_ts'] == start['ts'] for p in self.pending):
            return
        keys = list(self.candles.keys())
        current = self.candles[keys[current_idx]]
        log.info('SIGNAL %s | start=%s %s | trigger=%s %s', direction, utc_dt(start['ts']), color(start), utc_dt(current['ts']), color(current))
        emoji = 'ð¢' if direction == 'LONG' else 'ð´'
        telegram_send(
            f'{emoji} SIGNAL {direction}\n\n'
            f'ETHUSDT Futures\n'
            f'Timeframe: 10m\n'
            f'Start: {utc_dt(start["ts"])}\n'
            f'Trigger: candle 6\n\n'
            f'â ï¸ Signal only â no automatic trading.'
        )
        self.pending.append({'start_ts': start['ts'], 'start_idx': current_idx - 6, 'direction': direction})
        self.state['pending'] = self.pending
        save_state(self.state)


engine = SignalEngine(load_state())


def ws_loop():
    while True:
        try:
            log.info('WS connecting to %s', WS_URL)
            ws = websocket.create_connection(WS_URL, timeout=30, origin='https://www.mexc.com')
            ws.send(json.dumps({
                'method': 'sub.kline',
                'param': {'symbol': SYMBOL, 'interval': 'Min1'},
                'gzip': False,
            }))
            log.info('WS subscribed | %s Min1', SYMBOL)
            last_ping = time.time()
            minute_cache = {}

            while True:
                if time.time() - last_ping >= 15:
                    ws.send(json.dumps({'method': 'ping'}))
                    last_ping = time.time()
                ws.settimeout(5)
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    raise RuntimeError('WS closed by server')
                msg = json.loads(raw)
                if msg.get('channel') == 'pong':
                    continue
                if msg.get('channel') != 'push.kline':
                    continue
                d = msg.get('data') or {}
                if d.get('symbol') != SYMBOL:
                    continue
                ts = int(d['t'])
                minute_cache[ts] = {'ts': ts, 'open': float(d['o']), 'close': float(d['c']), 'source': 'WS'}

                # Every time a new minute arrives, close the previous 10m bucket when its window ended.
                completed = {}
                for mt, mc in list(minute_cache.items()):
                    b = bucket_10m(mt)
                    completed.setdefault(b, []).append(mc)
                now = int(time.time())
                for b, rows in sorted(completed.items()):
                    if b + 600 <= now and len(rows) >= 9:
                        c = {'ts': b, 'open': rows[0]['open'], 'close': rows[-1]['close'], 'count': len(rows)}
                        engine.on_closed_10m(c)
                        # remove old minutes for memory control
                        for mt in list(minute_cache):
                            if mt < b:
                                minute_cache.pop(mt, None)

        except Exception as e:
            log.exception('WS ERROR: %s | reconnecting in 5s', e)
            time.sleep(5)


def heartbeat_loop():
    while True:
        time.sleep(LOG_EVERY_SECONDS)
        latest = next(reversed(engine.candles.values())) if engine.candles else None
        if latest:
            age = int(time.time()) - (latest['ts'] + 600)
            log.info('HEARTBEAT | closed_10m=%d | latest=%s %s | age=%ss | pending=%d',
                     len(engine.candles), utc_dt(latest['ts']), color(latest), max(age, 0), len(engine.pending))
        else:
            log.info('HEARTBEAT | no closed 10m candles yet | pending=%d', len(engine.pending))


def main():
    log.info('Started ETH_USDT 10m signal bot v2 (REST seed + WebSocket)')
    log.info('Config: ws=enabled, symbol=%s, chat_id_configured=%s, token_configured=%s',
             SYMBOL, bool(TELEGRAM_CHAT_ID), bool(TELEGRAM_BOT_TOKEN))
    try:
        seed = parse_rest(fetch_rest_seed())
        engine.seed(seed)
    except Exception as e:
        log.exception('REST SEED ERROR: %s', e)
        log.info('Continuing with WebSocket only; waiting for fresh candles.')

    threading.Thread(target=heartbeat_loop, daemon=True).start()
    ws_loop()


if __name__ == '__main__':
    main()
