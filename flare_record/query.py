import re
from datetime import date, datetime, timedelta, timezone

from .db import connect 
from .setup import CLASS_RE

 
VALID_SOURCES = {"NOAA", "LMSAL"}
VALID_FAMILIES = {"A", "B", "C", "M", "X"}

def _goes_class_condition(GOESclass):
    # "X" = the whole family, "X1" = X1.0 through X1.9, "X1.0" = exactly 1.0 (old NOAA "X1" is stored as X1.0, so it matches too).
    match = CLASS_RE.fullmatch(GOESclass.strip().upper()) if isinstance(GOESclass, str) else None
    if not match:
        raise ValueError(f"GOESclass must look like 'X', 'X1' or 'X1.5', got {GOESclass!r}")
    letter, number = match.groups()
    if number is None:
        return "goes_family = ?", [letter]
    number = number.replace(",", ".")
    decimals = len(number.partition(".")[2])
    if decimals == 0:
        return "goes_family = ? AND goes_magnitude >= ? AND goes_magnitude < ?", [letter, int(number), int(number) + 1]
    return "goes_family = ? AND ROUND(goes_magnitude, ?) = ROUND(?, ?)", [letter, decimals, float(number), decimals]

def _active_region_number(activeRegion):
    # Both sources store the 4-digit NOAA number, so 13664, "13664" and "AR3664" all mean region 3664.
    text = str(activeRegion).strip().upper() if isinstance(activeRegion, (int, str)) and not isinstance(activeRegion, bool) else ""
    match = re.fullmatch(r"(?:AR\s*)?(\d+)", text)
    if not match or int(match.group(1)) % 10000 == 0:
        raise ValueError(f"activeRegion must be a region number like 13664 or 'AR3664', got {activeRegion!r}")
    return int(match.group(1)) % 10000

def _period(value):
    # Returns the (first, last) moment a timerange endpoint covers: "2024" is the whole year, "202405" the month,
    # "20240514" or a date the day, and a datetime just that moment (flare times are UTC).
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        value = value.replace(microsecond=0)
        return value, value
    if isinstance(value, date):
        start = datetime(value.year, value.month, value.day)
        return start, start + timedelta(days=1) - timedelta(seconds=1)
    text = value.strip() if isinstance(value, str) else ""
    if not re.fullmatch(r"\d{4}(?:[-/]?\d{2}){0,2}", text):
        raise ValueError(
            f"timerange dates must look like '2024', '202405', '20240514' or '2024-05-14', "
            f"or be a date/datetime, got {value!r}"
        )
    digits = re.sub(r"[-/]", "", text)
    formats = {4: "%Y", 6: "%Y%m", 8: "%Y%m%d"}
    try:
        start = datetime.strptime(digits, formats[len(digits)])
    except ValueError:
        raise ValueError(f"timerange date {value!r} is not a real date") from None
    if len(digits) == 4:
        next_start = start.replace(year=start.year + 1)
    elif len(digits) == 6:
        next_start = (start + timedelta(days=32)).replace(day=1)
    else:
        next_start = start + timedelta(days=1)
    return start, next_start - timedelta(seconds=1)

def _timerange_bounds(timerange):
    # One endpoint, or a (start, end) pair; the range runs from the start of the first to the end of the second.
    if isinstance(timerange, (list, tuple)):
        if len(timerange) != 2:
            raise ValueError(f"timerange must be one date or a (start, end) pair, got {len(timerange)} items: {timerange!r}")
        first, last = timerange
    else:
        first, last = timerange, timerange
    start = _period(first)[0]
    end = _period(last)[1]
    if start > end:
        raise ValueError(f"timerange start {first!r} is after end {last!r}")
    return start.isoformat(), end.isoformat()

def getData(source=None, timerange=None, flareType=None, GOESclass=None, activeRegion=None):
    """Returns matching flares from the local database as a list of dicts, sorted by begin time.

    source: "NOAA", "LMSAL", or a list of both; omit for both.
    timerange: "2024", "202405", "20240514", "2024-05-14", a date/datetime, or a (start, end) pair of those.
               A flare matches if it began inside the range (UTC).
    flareType: class family, "A" through "X".
    GOESclass: "X" = whole family, "X1" = X1.0 through X1.9, "X1.0" = exactly 1.0.
    activeRegion: NOAA region number, e.g. 13664, "3664" or "AR13664".

    All filters are optional and combine with AND. Invalid values raise ValueError.
    """
    conditions = []
    params = []
 
    if source is not None:
        # One source as a string, or several as a list, e.g. ["NOAA", "LMSAL"].
        sources = [source] if isinstance(source, str) else list(source) if isinstance(source, (list, tuple, set)) else None
        if not sources or not all(isinstance(s, str) and s.strip().upper() in VALID_SOURCES for s in sources):
            raise ValueError(f"source must be one of {sorted(VALID_SOURCES)} or a list of them, got {source!r}")
        sources = sorted({s.strip().upper() for s in sources})
        conditions.append(f"source IN ({', '.join('?' for _ in sources)})")
        params.extend(sources)
 
    if timerange is not None:
        # Filters on begin_time: a flare counts if it started inside the range.
        start, end = _timerange_bounds(timerange)
        conditions.append("begin_time BETWEEN ? AND ?")
        params.extend([start, end])
 
    if flareType is not None:
        family = flareType.strip().upper() if isinstance(flareType, str) else None
        if family not in VALID_FAMILIES:
            raise ValueError(f"flareType must be one of {sorted(VALID_FAMILIES)}, got {flareType!r}")
        conditions.append("goes_family = ?")
        params.append(family)
 
    if GOESclass is not None:
        condition, class_params = _goes_class_condition(GOESclass)
        conditions.append(condition)
        params.extend(class_params)

    if activeRegion is not None:
        conditions.append("active_region = ?")
        params.append(_active_region_number(activeRegion))
 
    query = "SELECT source, begin_time, peak_time, end_time, goes_class, active_region FROM flares"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY begin_time"
 
    conn = connect()
    try:
        cursor = conn.execute(query, params)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()
 