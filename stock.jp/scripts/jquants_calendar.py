"""Canonical inclusive calendar validation shared by collection and restoration."""
from datetime import date, timedelta


def validate(rows, start_text, end_text, *, complete, sessions=None):
    def canonical(value):
        if not isinstance(value, str): raise ValueError('invalid calendar date')
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value: raise ValueError('noncanonical calendar date')
        return parsed
    start, end = canonical(start_text), canonical(end_text)
    if not 0 <= (end-start).days <= 550: raise ValueError('invalid calendar range')
    expected = {(start+timedelta(days=n)).isoformat() for n in range((end-start).days+1)}
    found = {}
    for row in rows:
        if not isinstance(row, dict): raise ValueError('invalid calendar row')
        text = row.get('Date')
        canonical(text)
        holiday = str(row.get('HolDiv'))
        if text not in expected or text in found or holiday not in {'0','1','2','3'}:
            raise ValueError('duplicate or invalid calendar date')
        found[text] = holiday
    if complete and set(found) != expected: raise ValueError('calendar dates missing')
    if complete and sessions is not None and sum(v in {'1','2'} for v in found.values()) != sessions:
        raise ValueError('calendar session count differs from approval')
    return found
