"""
setup.py — builds flares.db from your LMSAL and NOAA source files.
 
This is the "download the db" step, adapted to what actually exists
right now: the raw data lives in your solar-events-dashboard repo
(events.json + noaa_data/). build_database() turns those into the single SQLite file getData() reads from.
 
download_database() is the function a user would call to just fetches a file to the right local path,
so no one needs the raw repo to use the package.
"""

import csv
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .db import DEFAULT_DB_PATH, get_db_path

# Older NOAA reports write classes as "X1", "C1.23", "C6,2" or a bare "C"; newer ones and LMSAL use "X1.0".
CLASS_RE = re.compile(r"([ABCMX])(\d+(?:[.,]\d+)?)?")

def _parse_class(text):
    # Returns (normalized class, magnitude) so "X1" and "X1.0" are stored the same way, or None if text isn't a GOES class.
    match = CLASS_RE.fullmatch(text.strip().upper())
    if not match:
        return None
    letter, number = match.groups()
    if number is None:
        return letter, None
    magnitude = float(number.replace(",", "."))
    decimals = len(number.replace(",", ".").partition(".")[2])
    return f"{letter}{magnitude:.{max(decimals, 1)}f}", magnitude

def _clean_hhmm(text):
    # NOAA prefixes uncertain times with a letter (B = before, A = after, U = uncertain); keep the time, drop the letter.
    match = re.fullmatch(r"[A-Z]?(\d{4})", text)
    if match and int(match.group(1)[:2]) < 24 and int(match.group(1)[2:]) < 60:
        return match.group(1)
    return None

def _combine(day, hhmm): 
    return day + timedelta(hours=int(hhmm[:2]), minutes=int(hhmm[2:]))

def _load_noaa_flares(noaa_dir): 
    flares=[]
    for filename in sorted(os.listdir(noaa_dir)): 
        if not re.fullmatch(r"\d{8}events\.txt", filename): # skip the files that doesnt follow the date format
            continue 
        file_date = datetime.strptime(filename[:8],"%Y%m%d")
        with open(os.path.join(noaa_dir,filename), errors="ignore") as f:
            for line in f:
                if line.startswith((":", "#")) or " XRA " not in line: 
                    continue
                columns = line.replace("+", " ").split()
                xra_position = columns.index("XRA")
                begin_hhmm, peak_hhmm, end_hhmm = (_clean_hhmm(c) for c in columns[1:4])
                parsed_class = _parse_class(columns[xra_position+2]) if len(columns) > xra_position + 2 else None
                region_field = columns[xra_position + 4] if len(columns) > xra_position + 4 else ""
                if not (begin_hhmm and parsed_class):
                    continue
                goes_class, goes_magnitude = parsed_class
                begin_dt = _combine(file_date, begin_hhmm)
                peak_dt = _combine(file_date, peak_hhmm) if peak_hhmm else None
                end_dt = _combine(file_date, end_hhmm) if end_hhmm else None
                if peak_dt and peak_dt < begin_dt:
                    peak_dt += timedelta(days=1)
                if end_dt and end_dt < begin_dt:
                    end_dt += timedelta(days=1)
                active_region = int(region_field) % 10000 if region_field.isdigit() else None
                flares.append({
                    "source": "NOAA", "begin_time": begin_dt.isoformat(),
                    "peak_time": peak_dt.isoformat() if peak_dt else None,
                    "end_time": end_dt.isoformat() if end_dt else None,
                    "goes_class": goes_class, "goes_magnitude": goes_magnitude,
                    "active_region": active_region,
                })
    return flares

def _time_after(begin_dt, hms_string):
    try:
        t = datetime.strptime(hms_string, "%H:%M:%S")
    except (ValueError, TypeError):
        return None
    combined = begin_dt.replace(hour=t.hour, minute=t.minute, second=0)
    return combined + timedelta(days=1) if combined < begin_dt else combined
 

def _load_lmsal_events(events_json_path):
    return _lmsal_rows(json.load(open(events_json_path)))

def _lmsal_rows(raw_events):
    # raw_events: dicts shaped like LMSAL's events.json entries (also what update_database() scrapes)
    events = []
    for ev in raw_events:
        parsed_class = _parse_class(ev.get("event_GOES") or "")
        if not parsed_class:
            continue
        goes_class, goes_magnitude = parsed_class
        try:
            begin_dt = datetime.strptime(ev["event_start"], "%Y/%m/%d %H:%M:%S")
        except (KeyError, ValueError, TypeError):
            continue
        peak_dt = _time_after(begin_dt, ev.get("event_peak"))
        end_dt = _time_after(begin_dt, ev.get("event_stop"))
        region_match = re.search(r"\(\s*(\d+)\s*\)", ev.get("event_position") or "")
        active_region = int(region_match.group(1)) % 10000 if region_match else None
        events.append({
            "source": "LMSAL", "begin_time": begin_dt.isoformat(),
            "peak_time": peak_dt.isoformat() if peak_dt else None,
            "end_time": end_dt.isoformat() if end_dt else None,
            "goes_class": goes_class, "goes_magnitude": goes_magnitude,
            "active_region": active_region,
        })
    return events

def _load_ncei_flares(csv_path):
    # NCEI's science-quality GOES flare report: one CSV row per flare.
    flares = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            parsed_class = _parse_class(row.get("flare_class") or "")
            try:
                begin_dt = datetime.fromisoformat((row.get("start_time") or "").strip())
            except ValueError:
                continue
            if not parsed_class:
                continue
            goes_class, goes_magnitude = parsed_class
            peak_dt = _iso_or_none(row.get("time"))
            end_dt = _iso_or_none(row.get("end_time"))
            region = (row.get("active_region") or "").strip()
            flares.append({
                "source": "NCEI", "begin_time": begin_dt.isoformat(),
                "peak_time": peak_dt, "end_time": end_dt,
                "goes_class": goes_class, "goes_magnitude": goes_magnitude,
                "active_region": int(region) % 10000 if region.isdigit() and int(region) % 10000 else None,
            })
    return flares

def _iso_or_none(text):
    try:
        return datetime.fromisoformat((text or "").strip()).isoformat()
    except ValueError:
        return None

def build_database(events_json_path, noaa_data_dir, output_path=None, ncei_csv_path=None):
    """Builds flares.db from local source files.

    events_json_path: path to LMSAL's events.json
    noaa_data_dir: path to the folder of noaa_data/*.txt files
    output_path: where to write the database (defaults to the standard
                 location get_db_path() looks for)
    ncei_csv_path: optional NCEI science-quality flare report (CSV); adds
                   its flares as source "NCEI"

    Returns the path the database was written to.
    """

    output_path = Path(output_path) if output_path else get_db_path()
    output_path.parent.mkdir(parents=True,exist_ok=True)

    if output_path.exists():
        output_path.unlink() # start clean rather than appending to stale data

    conn = sqlite3.connect(str(output_path))
    conn.execute("""
        CREATE TABLE flares (
            source TEXT NOT NULL,
            begin_time TEXT NOT NULL,
            peak_time TEXT,
            end_time TEXT,
            goes_class TEXT NOT NULL,
            goes_family TEXT NOT NULL,
            goes_magnitude REAL,
            active_region INTEGER
        )
    """)
        # Indexes make getData()'s filters fast even as the table grows; without them, every query would scan the entire table.

    conn.execute("CREATE INDEX idx_source ON flares(source)")
    conn.execute("CREATE INDEX idx_begin ON flares(begin_time)")
    conn.execute("CREATE INDEX idx_family ON flares(goes_family)")
    conn.execute("CREATE INDEX idx_class ON flares(goes_class)")
    conn.execute("CREATE INDEX idx_region ON flares(active_region)")


    print(f"Loading LMSAL events from {events_json_path}...")
    lmsal_rows = _load_lmsal_events(events_json_path)
    print(f"  {len(lmsal_rows)} events")
 
    print(f"Loading NOAA flares from {noaa_data_dir}...")
    noaa_rows = _load_noaa_flares(noaa_data_dir)
    print(f"  {len(noaa_rows)} flares")

    ncei_rows = []
    if ncei_csv_path:
        print(f"Loading NCEI flares from {ncei_csv_path}...")
        ncei_rows = _load_ncei_flares(ncei_csv_path)
        print(f"  {len(ncei_rows)} flares")

    # Some NOAA files repeat the same event line; keep one copy of rows that are identical in every column.
    all_rows = list(dict.fromkeys(
        (r["source"], r["begin_time"], r["peak_time"], r["end_time"],
         r["goes_class"], r["goes_class"][0], r["goes_magnitude"], r["active_region"])
        for r in lmsal_rows + noaa_rows + ncei_rows
    ))
    conn.executemany(
        "INSERT INTO flares (source, begin_time, peak_time, end_time, "
        "goes_class, goes_family, goes_magnitude, active_region) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        all_rows
    )
    conn.commit()
    conn.close()
 
    print(f"\nWrote {len(all_rows)} rows to {output_path}")
    return output_path

def download_database(url, output_path=None):
    #Downloads a pre-built flares.db from url to the local database location. Use this once a compiled database is hosted somewhere (e.g. a GitHub Release asset); until then, use build_database() against a local copy of the source data instead.
    import httpx
 
    output_path = Path(output_path) if output_path else get_db_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
 
    print(f"Downloading {url} -> {output_path}")
    with httpx.stream("GET", url, timeout=60, follow_redirects=True) as response:
        response.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
 
    print("Download complete.")
    return output_path


def _build_cli():
    # Entry point for the `flare-record-build` command. Reads paths from environment variables so the command needs no arguments in the common case (running it inside the dashboard repo).
    events_json = os.environ.get("FLARE_RECORD_EVENTS_JSON", os.path.join("data", "events.json"))
    noaa_dir = os.environ.get("FLARE_RECORD_NOAA_DIR", os.path.join("data", "noaa_data"))
    if not os.path.exists(events_json):
        raise SystemExit(
            f"Can't find {events_json}. Run this from the solar-events-dashboard "
            f"repo root, or set FLARE_RECORD_EVENTS_JSON to its path."
        )
    if not os.path.isdir(noaa_dir):
        raise SystemExit(
            f"Can't find the {noaa_dir}/ folder. Run this from the "
            f"solar-events-dashboard repo root, or set FLARE_RECORD_NOAA_DIR."
        )
    # optional: NCEI's science-quality report, from the env var or the dashboard repo's data/goes_sciquality/
    ncei_csv = os.environ.get("FLARE_RECORD_NCEI_CSV")
    if not ncei_csv:
        found = sorted(Path("data", "goes_sciquality").glob("sci_xrsf-l2-flrpt_geo_*.csv"))
        ncei_csv = str(found[-1]) if found else None
    build_database(events_json, noaa_dir, ncei_csv_path=ncei_csv)