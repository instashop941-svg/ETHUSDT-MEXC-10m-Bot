import os, json, time, logging
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
CHAT_IDS=[x.strip() for x in os.getenv('TELEGRAM_CHAT_ID','').split(',') if x.strip()]
SYMBOLS=[x.strip().upper() for x in os.getenv('MEXC_SYMBOLS','ETH_USDT,BTC_USDT').split(',') if x.strip()]
POLL=int(os.getenv('POLL_SECONDS','15'))
STATE_FILE=os.getenv('STATE_FILE','state.json')
URL='https://api.mexc.com/api/v1/contract/kline/{symbol}'

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log=logging.getLogger('eth-bot')

def utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

def kyiv(ts):
    return datetime.fromtimestamp(ts, tz=ZoneInfo('Europe/Kyiv')).strftime('%Y-%m-%d %H:%M')

def color(c):
    return 'GREEN' if c['close'] > c['open'] else 'RED' if c['close'] < c['open'] else 'DOJI'

def load_state(path=None):
    path=path or STATE_FILE
    try:
        with open(path, encoding='utf8') as f:
            return json.load(f)
    except Exception:
        return {'last_processed_10m': None, 'pending': []}

def save_state(s, path=None):
    path=path or STATE_FILE
    with open(path + '.tmp', 'w', encoding='utf8') as f:
        json.dump(s, f, indent=2)
    os.replace(path + '.tmp', path)

def tg(text, group_text=None):
    if not TOKEN or not CHAT_IDS:
        log.error('TELEGRAM NOT CONFIGURED')
        return False
    sent=0
    for chat_id in CHAT_IDS:
        try:
            send_text = group_text if (group_text is not None and chat_id.startswith('-')) else text
            r=requests.post(
                f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                json={'chat_id': chat_id, 'text': send_text},
                timeout=15
            )
            if r.ok and r.json().get('ok'):
                sent += 1
                log.info('TELEGRAM SENT OK | chat=%s', chat_id)
            else:
                log.error('TELEGRAM ERROR | chat=%s | status=%s body=%s', chat_id, r.status_code, r.text[:300])
        except Exception as e:
            log.exception('TELEGRAM EXCEPTION | chat=%s: %s', chat_id, e)
    return sent == len(CHAT_IDS)

def fetch(symbol):
    r=requests.get(
        URL.format(symbol=symbol),
        params={'interval':'Min1', 'limit':300, '_ts':int(time.time()*1000)},
        headers={'Cache-Control':'no-cache', 'Pragma':'no-cache', 'User-Agent':'ETHUSDT-10m-Signal-Bot/4.0'},
        timeout=15
    )
    r.raise_for_status()
    p=r.json()
    data=p.get('data')
    if not data:
        raise RuntimeError(f'MEXC empty response: {p}')
    out=[]
    if isinstance(data, dict) and isinstance(data.get('time'), list):
        times=data['time']; opens=data.get('open',[]); closes=data.get('close',[])
        for i,t in enumerate(times):
            out.append({'ts':int(t), 'open':float(opens[i]), 'close':float(closes[i])})
    elif isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                out.append({'ts':int(row.get('time',row.get('t'))), 'open':float(row.get('open',row.get('o'))), 'close':float(row.get('close',row.get('c')))})
            else:
                out.append({'ts':int(row[0]), 'open':float(row[1]), 'close':float(row[2])})
    else:
        raise RuntimeError(f'Unknown MEXC data format: {type(data).__name__}')
    out.sort(key=lambda x:x['ts'])
    return out

def current_10m(mins):
    """Build the currently forming 10m candle from 1m data."""
    if not mins:
        return None
    b=(mins[-1]['ts']//600)*600
    rows=[x for x in mins if (x['ts']//600)*600==b]
    if not rows:
        return None
    rows.sort(key=lambda x:x['ts'])
    return {'ts':b, 'open':rows[0]['open'], 'close':rows[-1]['close'], 'count':len(rows)}

def agg(mins):
    buckets=OrderedDict()
    for c in mins:
        b=(c['ts']//600)*600
        buckets.setdefault(b,[]).append(c)
    now=int(time.time())
    out=[]
    for b,rows in buckets.items():
        if b+600<=now and len(rows)>=9:
            out.append({'ts':b, 'open':rows[0]['open'], 'close':rows[-1]['close'], 'count':len(rows)})
    return out

class Engine:
    def __init__(self, state, state_file, symbol):
        self.state=state
        self.state_file=state_file
        self.symbol=symbol
        self.c=OrderedDict()
        self.pending=[]
        self.initialized=False
        self.pre_alerted=set()

    def seed(self, closed):
        """Load current history without generating historical signals/results."""
        self.c=OrderedDict((x['ts'], x) for x in closed[-150:])
        self.c=OrderedDict(sorted(self.c.items()))
        self.pending=[]
        self.state['pending']=[]
        self.state['last_processed_10m']=next(reversed(self.c)) if self.c else None
        save_state(self.state, self.state_file)
        self.initialized=True
        if self.c:
            latest=next(reversed(self.c.values()))
            log.info('INITIALIZED | history=%d | latest=%s %s | waiting for NEW 10m candle', len(self.c), utc(latest['ts']), color(latest))

    def maybe_pre_alert(self, live10):
        """At ~2 minutes before trigger close, warn when the live trigger is opposite the start."""
        if not self.initialized or not live10:
            return
        # Only send during the final ~2 minutes of the current 10m candle.
        now=time.time()
        elapsed=now-live10['ts']
        if elapsed < 480 or elapsed >= 600:
            return

        keys=list(self.c)
        # Need the five prior closed candles plus the start candle; trigger is live.
        # Current live candle is candle #6 after the candidate start.
        target_ts=live10['ts']
        if target_ts in self.c:
            return
        s_ts=target_ts-6*600
        if s_ts not in self.c:
            return
        sidx=keys.index(s_ts)
        start=self.c[s_ts]
        sc=color(start)
        if sc not in ('GREEN','RED'):
            return
        # Start must be the last candle of its same-color run.
        if sidx+1 < len(keys) and color(self.c[keys[sidx+1]]) == sc:
            return

        trig=color(live10)
        if trig not in ('GREEN','RED') or trig == sc:
            return
        if target_ts in self.pre_alerted:
            return

        log.info('PRE-SIGNAL | start=%s %s | live trigger=%s %s | ~2m left', utc(start['ts']), sc, utc(target_ts), trig)
        group_text=(f'**Всі готові?**\n'
                     f'**Скоро дам СИГНАЛ!**\n\n'
                     f'{self.symbol.replace("_USDT","USDT")} Futures\n'
                     'Timeframe: 10m\n\n'
                     '⚠️ Сигнал буде тільки після закриття свічки.')
        # Pre-signal announcement is intended for the Telegram group only.
        tg('', group_text)
        self.pre_alerted.add(target_ts)

    def ingest_new(self, closed):
        if not self.initialized:
            self.seed(closed)
            return 0
        known=set(self.c)
        new=[x for x in closed if x['ts'] not in known]
        for x in new:
            self.c[x['ts']]=x
        self.c=OrderedDict(sorted(self.c.items()))
        while len(self.c)>150:
            self.c.popitem(last=False)
        for x in new:
            self.evaluate(x['ts'])
        if new:
            self.state['pending']=self.pending
            self.state['last_processed_10m']=new[-1]['ts']
            save_state(self.state, self.state_file)
        return len(new)

    def evaluate(self, ts):
        keys=list(self.c)
        idx=keys.index(ts)
        cur=self.c[ts]

        # Pending signals: #6 is the trigger; #7 and #8 must stay the
        # same color as #6. Result control is ONLY candles #9..#15.
        keep=[]
        for p in self.pending:
            rel=idx-p['trigger_idx']

            # #7 and #8 must match the trigger color. If either changes,
            # the setup is invalid and no final signal is sent.
            trigger_color=p['trigger_color']
            if rel in (1, 2):
                if color(cur) != trigger_color:
                    log.info('CANCEL SETUP | start=%s | trigger=%s | candle=%d changed color', utc(p['start_ts']), trigger_color, rel+6)
                else:
                    # After #8 closes successfully, send the final signal.
                    if rel == 2 and not p.get('signal_sent', False):
                        signal_text=(f'SIGNAL {p["direction"]}\n\n{self.symbol.replace("_USDT","USDT")} Futures\nTimeframe: 10m\nStart: {kyiv(p["start_ts"])} Kyiv time\nTrigger: candle 6\n\nSignal only - no automatic trading.\n\nТрейдер Василь Павлів\n@vasylpavliv\nhttps://t.me/vasylpavliv')
                        signal_group_text=(f'SIGNAL {p["direction"]}\n\n{self.symbol.replace("_USDT","USDT")} Futures\nTimeframe: 10m\nStart: {kyiv(p["start_ts"])} Kyiv time\n\nSignal only - no automatic trading.\n\nТрейдер Василь Павлів\n@vasylpavliv\nhttps://t.me/vasylpavliv')
                        tg(signal_text, signal_group_text)
                        p['signal_sent']=True
                    keep.append(p)
                continue

            # Result is checked only on candles #9..#15.
            if rel < 3:
                keep.append(p)
                continue

            want='GREEN' if p['direction']=='LONG' else 'RED'
            if rel <= 9 and color(cur)==want:
                log.info('RESULT WIN | %s | start=%s | result candle=%d', p['direction'], utc(p['start_ts']), rel+6)
                tg(f'WIN\n{self.symbol.replace('_USDT','USDT')} Futures\nDirection: {p["direction"]}\nResult candle: {rel+6}/15\n\nТрейдер Василь Павлів\n@vasylpavliv\nhttps://t.me/vasylpavliv')
            elif rel >= 9:
                log.info('RESULT LOSS | %s | start=%s | no confirmation in candles 9-15', p['direction'], utc(p['start_ts']))
                tg(f'LOSS\n{self.symbol.replace('_USDT','USDT')} Futures\nDirection: {p["direction"]}\nNo confirmation in candles 9-15\n\nТрейдер Василь Павлів\n@vasylpavliv\nhttps://t.me/vasylpavliv')
            else:
                keep.append(p)
        self.pending=keep

        # Start can be either:
        #   1) an isolated GREEN/RED candle, or
        #   2) the LAST candle of a consecutive run of the same color.
        #
        # Trigger is exactly the 6th subsequent candle. Candles #7 and #8
        # must keep the same color as the trigger. Candles #9..#15 are
        # reserved only for the result check.
        if idx<6:
            return
        sidx=idx-6
        start=self.c[keys[sidx]]
        sc=color(start)
        if sc not in ('GREEN','RED'):
            return

        # If the next candle has the same color, this is NOT the last candle
        # of the run, so this candidate start is ignored. If the next candle
        # is opposite color or DOJI, this candle IS the last one of its run.
        if sidx+1 < len(keys) and color(self.c[keys[sidx+1]]) == sc:
            return

        trig=color(self.c[keys[idx]])
        direction='LONG' if sc=='GREEN' and trig=='RED' else 'SHORT' if sc=='RED' and trig=='GREEN' else None
        if not direction:
            return
        if any(p['start_ts']==start['ts'] for p in self.pending):
            return
        log.info('TRIGGER CANDIDATE %s | start=%s %s | trigger=%s %s | waiting for #7-#8', direction, utc(start['ts']), sc, utc(ts), trig)
        self.pending.append({'start_ts':start['ts'], 'trigger_idx':idx, 'trigger_color':trig, 'direction':direction, 'signal_sent':False})

engines={}
for sym in SYMBOLS:
    sf=os.getenv(f'STATE_FILE_{sym}', f'state_{sym}.json')
    engines[sym]=Engine(load_state(sf), sf, sym)

def main():
    log.info('Started BTCUSDT + ETHUSDT 10m signal bot v4-fixed (REST polling)')
    log.info('Config: poll=%ss, chats=%d, token_configured=%s', POLL, len(CHAT_IDS), bool(TOKEN))
    log.info('Symbols: %s', ', '.join(SYMBOLS))
    log.info('Rule: #1 Start | #6 trigger | #7-#8 same color as #6 | #9-#15 result')
    if TOKEN and CHAT_IDS:
        tg('BOT ONLINE\nBTCUSDT + ETHUSDT Futures\nSignal bot is active.\nThis test confirms Telegram delivery to all configured chats.')
    last_log={}
    while True:
        for symbol,engine in engines.items():
            try:
                mins=fetch(symbol)
                live10=current_10m(mins)
                if live10:
                    engine.maybe_pre_alert(live10)
                closed=agg(mins)
                if not closed:
                    log.warning('%s | MEXC OK but no closed 10m candles yet', symbol)
                else:
                    latest=closed[-1]
                    n=engine.ingest_new(closed)
                    if n:
                        log.info('NEW DATA | %s | MEXC 1m=%d | closed_10m=%d | added=%d | latest=%s %s | O=%.4f C=%.4f | pending=%d', symbol, len(mins), len(closed), n, utc(latest['ts']), color(latest), latest['open'], latest['close'], len(engine.pending))
                    elif time.time()-last_log.get(symbol,0)>=60:
                        age=int(time.time()-(latest['ts']+600))
                        log.info('HEARTBEAT OK | %s | MEXC 1m=%d | closed_10m=%d | latest=%s %s | age=%ss | pending=%d', symbol, len(mins), len(closed), utc(latest['ts']), color(latest), max(age,0), len(engine.pending))
                        last_log[symbol]=time.time()
            except Exception as e:
                log.exception('LOOP ERROR %s: %s', symbol, e)
        time.sleep(POLL)

if __name__=='__main__':
    main()
