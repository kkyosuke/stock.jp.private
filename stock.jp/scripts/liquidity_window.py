"""Fixed official-session liquidity denominator; no price-dependent selection."""
from datetime import date, timedelta
try:
    from scripts.jquants_snapshot import bar_diagnostic
    from scripts.price_snapshot import normalize_code
except ModuleNotFoundError:
    from jquants_snapshot import bar_diagnostic
    from price_snapshot import normalize_code


def trading_value_window(bars, calendar, *, code, cutoff, sessions=20):
    if sessions != 20:
        raise ValueError('liquidity requires exactly 20 official trading sessions')
    dates={}
    for row in calendar:
        day=date.fromisoformat(row['date']);division=row['holiday_division']
        if day in dates or division not in {'0','1','2','3'}:raise ValueError('invalid official calendar')
        dates[day]=division
    window=[];day=cutoff
    for _ in range(60):
        if day not in dates:raise ValueError('official calendar day missing')
        if dates[day] in {'1','2'}:window.append(day.isoformat())
        if len(window)==sessions:break
        day-=timedelta(days=1)
    if len(window)!=sessions:raise ValueError('insufficient official sessions')
    by_day={}
    for row in bars:
        if normalize_code(row.get('Code'))!=code or row.get('Date') not in window:continue
        if row['Date'] in by_day:raise ValueError('duplicate daily bar')
        by_day[row['Date']]=row
    values=[]
    for day_text in sorted(window):
        row=by_day.get(day_text);status=bar_diagnostic(row)
        if status in {'ROW_MISSING','PARTIAL_NULL_OR_INVALID'}:raise ValueError('missing or invalid bar in fixed liquidity window')
        # Joint explicit null means no on-session execution, hence zero traded
        # value in this session. Preserve null in raw; never impute a close.
        values.append(0.0 if status=='NO_EXECUTION' else float(row['Va']))
    if bar_diagnostic(by_day[window[0]])!='VALID_PRICE':
        raise ValueError('latest session has no valid execution price')
    return sorted(window),values
