import os, json, time, logging
from collections import OrderedDict
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv

load_dotenv()
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
CHAT_IDS=[x.strip() for x in os.getenv('TELEGRAM_CHAT_ID','').split(',') if x.strip()]
SYMBOL=os.getenv('MEXC_SYMBOL','ETH_USDT').strip().upper()
POLL=int(os.getenv('POLL_SECONDS','15'))
STATE_FILE=os.getenv('STATE_FILE','state.json')
URL=f'https://api.mexc.com/api/v1/contract/kline/{SYMBOL}'

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log=logging.getLogger('eth-bot')

def utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

def color(c):
    return 'GREEN' if c['close'] > c['open'] else 'RED' if c['close'] < c['open'] else 'DOJI'

def load_state():
    try:
        with open(STATE_FILE, encoding='utf8') as f:
            return json.load(f)
    except Exception:
        return {'last_processed_10m': None, 'pending': []}

def save_state(s):
    with open(STATE_FILE + '.tmp', 'w', encoding='utf8') as f:
        json.dump(s, f, indent=2)
    os.replace(STATE_FILE + '.tmp', STATE_FILE)

def tg(text):
    if not TOKEN or not CHAT_IDS:
        log.error('TELEGRAM NOT CONFIGURED')
        return False

    sent = 0
    for chat_id in CHAT_IDS:
        try:
            r=requests.post(
                f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                json={'chat_id': chat_id, 'text': text},
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

def fetch():
    r=requests.get(
        URL,
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
    def __init__(self, state):
        self.state=state
        self.c=OrderedDict()
        self.pending=[]
        self.initialized=False

    def seed(self, closed):
        """Load current history without generating historical signals/results."""
        self.c=OrderedDict((x['ts'], x) for x in closed[-150:])
        self.c=OrderedDict(sorted(self.c.items()))
        self.pending=[]
        self.state['pending']=[]
        self.state['last_processed_10m']=next(reversed(self.c)) if self.c else None
        save_state(self.state)
        self.initialized=True
        if self.c:
            latest=next(reversed(self.c.values()))
            log.info('INITIALIZED | history=%d | latest=%s %s | waiting for NEW 10m candle', len(self.c), utc(latest['ts']), color(latest))

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
            save_state(self.state)
        return len(new)

    def evaluate(self, ts):
        keys=list(self.c)
        idx=keys.index(ts)
        cur=self.c[ts]

        # Existing pending signals: control candles 7..13 correspond to rel 1..7.
        keep=[]
        for p in self.pending:
            rel=idx-p['trigger_idx']
            if rel<1:
                keep.append(p)
                continue
            want='GREEN' if p['direction']=='LONG' else 'RED'
            if color(cur)==want:
                log.info('RESULT WIN | %s | start=%s | control=%d', p['direction'], utc(p['start_ts']), rel)
                tg(f'WIN\nETHUSDT Futures\nDirection: {p["direction"]}\nControl candle: {rel}/7')
            elif rel>=7:
                log.info('RESULT LOSS | %s | start=%s', p['direction'], utc(p['start_ts']))
                tg(f'LOSS\nETHUSDT Futures\nDirection: {p["direction"]}\nNo confirmation in candles 7-13')
            else:
                keep.append(p)
        self.pending=keep

        # Start = last candle of a consecutive same-color run.
        # Trigger = exactly the 6th subsequent candle, opposite color.
        if idx<6:
            return
        sidx=idx-6
        start=self.c[keys[sidx]]
        seq=[self.c[keys[sidx+j]] for j in range(1,7)]
        sc=color(start)
        if sc not in ('GREEN','RED'):
            return
        if color(seq[0])==sc:
            return
        trig=color(seq[-1])
        direction='LONG' if sc=='GREEN' and trig=='RED' else 'SHORT' if sc=='RED' and trig=='GREEN' else None
        if not direction:
            return
        if any(p['start_ts']==start['ts'] for p in self.pending):
            return
        log.info('SIGNAL %s | start=%s %s | trigger=%s %s', direction, utc(start['ts']), sc, utc(ts), trig)
        tg(f'SIGNAL {direction}\n\nETHUSDT Futures\nTimeframe: 10m\nStart: {utc(start["ts"])}\nTrigger: candle 6\n\nSignal only - no automatic trading.')
        self.pending.append({'start_ts':start['ts'], 'trigger_idx':idx, 'direction':direction})

state=load_state()
engine=Engine(state)

def main():
    log.info('Started ETH_USDT 10m signal bot v4 (REST polling)')
    log.info('Config: poll=%ss, chats=%d, token_configured=%s', POLL, len(CHAT_IDS), bool(TOKEN))
    last_log=0
    while True:
        try:
            mins=fetch()
            closed=agg(mins)
            if not closed:
                log.warning('MEXC OK but no closed 10m candles yet')
            else:
                latest=closed[-1]
                n=engine.ingest_new(closed)
                if n:
                    log.info('NEW DATA | MEXC 1m=%d | closed_10m=%d | added=%d | latest=%s %s | O=%.4f C=%.4f | pending=%d', len(mins), len(closed), n, utc(latest['ts']), color(latest), latest['open'], latest['close'], len(engine.pending))
                elif time.time()-last_log>=60:
                    age=int(time.time()-(latest['ts']+600))
                    log.info('HEARTBEAT OK | MEXC 1m=%d | closed_10m=%d | latest=%s %s | age=%ss | pending=%d', len(mins), len(closed), utc(latest['ts']), color(latest), max(age,0), len(engine.pending))
                    last_log=time.time()
        except Exception as e:
            log.exception('LOOP ERROR: %s', e)
        time.sleep(POLL)

if __name__=='__main__':
    main()
