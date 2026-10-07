"""Non-overlapping 24-hour account evaluations, anchored to initial funding."""
from datetime import datetime, timedelta, timezone
import json


def _iso(value):
    return value.isoformat(timespec='seconds')


def model_usage_totals(scans):
    """Aggregate reported usage only; absent usage is not a zero-cost call."""
    totals = {}
    for scan in scans:
        for role in ('retrospective', 'correlation'):
            entry = scan.get('model_usage', {}).get(role) or {}
            usage = entry.get('usage')
            if not isinstance(usage, dict):
                continue
            key = (role, entry.get('model', 'unknown'))
            row = totals.setdefault(key, dict(role=role, model=key[1],
                calls_with_usage=0, input_tokens=0, output_tokens=0, total_tokens=0))
            row['calls_with_usage'] += 1
            incoming = usage.get('input_tokens', usage.get('prompt_tokens', 0)) or 0
            outgoing = usage.get('output_tokens', usage.get('completion_tokens', 0)) or 0
            row['input_tokens'] += incoming
            row['output_tokens'] += outgoing
            row['total_tokens'] += usage.get('total_tokens', incoming + outgoing) or 0
    return list(totals.values())


def period_report(store, start, end, *, current_equity=None, include_evidence=True):
    with store.lock:
        ledger = 'live' if store._get('mode') == 'live' else 'paper'
        first = store.conn.execute('SELECT ts,equity FROM equity WHERE ledger = ? AND ts <= ? ORDER BY id DESC LIMIT 1', (ledger, _iso(start))).fetchone()
        last = store.conn.execute('SELECT ts,equity FROM equity WHERE ledger = ? AND ts <= ? ORDER BY id DESC LIMIT 1', (ledger, _iso(end))).fetchone()
        scan_rows = store.conn.execute('SELECT payload FROM scans WHERE ts >= ? AND ts < ?', (_iso(start),_iso(end))).fetchall() if include_evidence else []
        changes = store.conn.execute('SELECT actor,action,reason FROM audit WHERE ts >= ? AND ts < ?', (_iso(start),_iso(end))).fetchall() if include_evidence else []
        trades = store.conn.execute('SELECT action,fee,pnl FROM trades WHERE ledger = ? AND ts >= ? AND ts < ?', (ledger, _iso(start),_iso(end))).fetchall()
    scans = [json.loads(r[0]) for r in scan_rows]
    scan_totals = {}
    for scan in scans:
        for key, value in scan.get('counts', {}).items():
            scan_totals[key] = scan_totals.get(key, 0) + value
    if first:
        opening = float(first['equity'])
    elif ledger == 'live':
        opening = float(store._get('live_budget') or 0)
    else:
        opening = store.bankroll
    closing = float(current_equity) if current_equity is not None else float(last['equity']) if last else opening
    realized = sum(float(t['pnl']) for t in trades if t['action'] in {'sell','settle'})
    pnl = closing-opening
    return {
        'start':_iso(start), 'end':_iso(end), 'opening_equity':opening, 'closing_equity':closing,
        'net_pnl':round(pnl,6), 'return_pct':round(pnl/opening*100,6) if opening else None,
        'realized_pnl':round(realized,6), 'unrealized_change':round(pnl-realized,6),
        'operating_costs_included':False,
        'model_usage':model_usage_totals(scans) if include_evidence else None,
        'operating_cost_usd':None,
        'model_usage_note':'Reported token usage only; missing usage and unknown billing are not zero cost.',
        'fees_paid':round(sum(float(t['fee']) for t in trades),6),
        'buys':sum(t['action']=='buy' for t in trades),
        'closed_trades':sum(t['action'] in {'sell','settle'} for t in trades),
        'losing_closes':sum(t['action'] in {'sell','settle'} and t['pnl']<0 for t in trades),
        'status':'negative' if pnl < -1e-6 else 'positive' if pnl > 1e-6 else 'flat',
        'opening_mark_at':first['ts'] if first else None,
        'closing_mark_age_seconds':0 if current_equity is not None else (end-datetime.fromisoformat(last['ts'])).total_seconds() if last else None,
        'closing_mark_at':_iso(end) if current_equity is not None else last['ts'] if last else None,
        'scan_totals':scan_totals, 'scan_count':len(scans),
        'scan_errors':[error for scan in scans for error in scan.get('errors',[])],
        'changes':[dict(row) for row in changes],
        'boundary_note':'Uses the latest available mark at or before each boundary; fees are already included in net P&L. Model/API operating costs are separate and not deducted. Flat with no trades is not evidence of profitability.',
    }


def evaluate_days(store, now=None):
    now=now or datetime.now(timezone.utc)
    with store.lock:
        started=datetime.fromisoformat(store._get('paper_started_at'))
        completed=max(0,int((now-started).total_seconds()//86400))
        for day in range(1,completed+1):
            if store.conn.execute('SELECT 1 FROM daily_reviews WHERE day=?',(day,)).fetchone():
                continue
            start=started+timedelta(days=day-1)
            row=period_report(store,start,start+timedelta(days=1))
            row['day']=day
            row['review_required']=row['status']=='negative'
            row['review_status']='pending'
            store.conn.execute('INSERT INTO daily_reviews(day,payload) VALUES (?,?)',(day,json.dumps(row)))
            store._audit('daily','evaluate',None,row,
                f"Day {day}: net ${row['net_pnl']:.2f} after fees. " + ('Investigate execution, concentration, calibration, fees and ordinary variance; do not change rules solely because one day lost.' if row['review_required'] else 'Daily target met; assess evidence before inferring strategy quality.'))
        store._commit()
        pending=store.conn.execute('SELECT payload FROM daily_reviews ORDER BY day DESC LIMIT 30').fetchall()
    return [json.loads(r[0]) for r in pending]


def current_day(store, equity, now=None):
    now=now or datetime.now(timezone.utc)
    with store.lock:
        started=datetime.fromisoformat(store._get('paper_started_at'))
    day=max(0,int((now-started).total_seconds()//86400))
    start=started+timedelta(days=day)
    row=period_report(store,start,now,current_equity=equity,include_evidence=False)
    row.update(day=day+1, ends_at=_iso(start+timedelta(days=1)), complete=False,
               initial_capital=store.bankroll, net_contributions=store.bankroll,
               capital_change=round(equity-store.bankroll,6))
    return row


def save_reviews(store, days, summary, concerns, error):
    with store.lock:
        for row in days:
            if row.get('review_status') != 'pending':
                continue
            row.update(review_status='pending' if error else 'reviewed', review_summary=summary,
                       concerns=concerns, review_error=error)
            store.conn.execute('UPDATE daily_reviews SET payload=? WHERE day=?',(json.dumps(row),row['day']))
        store._commit()


def rolling_day(store, equity, now=None):
    now = now or datetime.now(timezone.utc)
    with store.lock:
        started = datetime.fromisoformat(store._get('paper_started_at'))
    start = max(started, now-timedelta(hours=24))
    row = period_report(store, start, now, current_equity=equity, include_evidence=False)
    row['multiple'] = equity/row['opening_equity'] if row['opening_equity'] else None
    row['partial'] = now-started < timedelta(hours=24)
    return row
