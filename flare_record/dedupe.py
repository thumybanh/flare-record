"""
dedupe.py — merges LMSAL entries that are the same flare listed more than once.

Same rule as the solar-events-dashboard. LMSAL builds an event's ID from its start time, so when it
lists one flare twice with start times a few minutes apart (or corrects the start and lists it
again), the flare gets two IDs. Two entries are the same flare when they have the same GOES class,
the same peak time and the same position; the "( AR )" part of the position is ignored, since one
entry often has the region number and the other doesn't. Entries whose positions differ are
different flares, even if class and peak time agree.

The merged record is LMSAL's latest version (the entry still on its most recent snapshot); if LMSAL
kept listing both to the end, the earlier start. The other entries are kept inside it under
merged_from, so a caller can still see every start time LMSAL gave.
"""

import copy
import re
from datetime import datetime, timedelta

ENTRY_FIELDS = ('event_id', 'event_start', 'event_stop', 'event_peak', 'event_GOES', 'event_position',
                'event_url', 'seen_in_dates')


def _start(event):
    return datetime.strptime(event['event_start'], '%Y/%m/%d %H:%M:%S')


def _peak(event):
    """Peak as a full datetime (event_peak is a bare clock time; earlier than the start means the next day)."""
    try:
        start = _start(event)
        clock = datetime.strptime(event.get('event_peak') or '', '%H:%M:%S')
    except ValueError:
        return None
    peak = start.replace(hour=clock.hour, minute=clock.minute, second=clock.second)
    return peak + timedelta(days=1) if peak < start else peak


def position_without_ar(position):
    return re.sub(r'\s*\(.*?\)\s*', '', position or '').strip()


def _has_ar(position):
    return bool(re.search(r'\(\s*\d+\s*\)', position or ''))


def _last_listed(event):
    """The newest LMSAL snapshot ('YYYYMMDD_HHMM') that listed this entry."""
    stamps = [m.group(1) for u in event.get('seen_in_dates') or [] for m in [re.search(r'last_events_(\d{8}_\d{4})', u)] if m]
    return max(stamps, default='')


def _entry(event):
    return {k: copy.deepcopy(event.get(k)) for k in ENTRY_FIELDS}


def _choose_kept(group):
    already = [e for e in group if e.get('merged_from')]
    if already:
        return min(already, key=_start)  # merged before: it stays the flare's record
    newest = max(_last_listed(e) for e in group)
    return min((e for e in group if _last_listed(e) == newest), key=_start)


def _merge_group(group):
    kept = _choose_kept(group)
    entries = {m['event_id']: m for m in kept.get('merged_from') or []}
    for event in group:
        if event is kept:
            continue
        for incoming in [_entry(event)] + list(event.get('merged_from') or []):
            if incoming['event_id'] == kept['event_id']:
                continue
            known = entries.get(incoming['event_id'])
            if known is None:
                entries[incoming['event_id']] = incoming
            else:  # an entry already merged turned up again: just extend its snapshot history
                known['seen_in_dates'] = known.get('seen_in_dates') or []
                known['seen_in_dates'] += [u for u in incoming.get('seen_in_dates') or [] if u not in known['seen_in_dates']]
    kept['merged_from'] = sorted(entries.values(), key=lambda m: m['event_start'])
    if not _has_ar(kept.get('event_position')):
        with_ar = next((m['event_position'] for m in kept['merged_from'] if _has_ar(m['event_position'])), None)
        if with_ar:
            kept['event_position'] = with_ar
    return kept


def merge_duplicates(events):
    """Returns the events with each flare listed once; order is kept and unduplicated events are untouched."""
    events = copy.deepcopy(events)
    groups = {}
    for event in events:
        peak = _peak(event)
        if peak is not None:
            key = ((event.get('event_GOES') or '').upper(), peak, position_without_ar(event.get('event_position')))
            groups.setdefault(key, []).append(event)
    dropped = set()
    for group in groups.values():
        if len(group) > 1:
            kept = _merge_group(group)
            dropped |= {id(e) for e in group if e is not kept}
    return [e for e in events if id(e) not in dropped]
