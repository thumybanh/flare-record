"""
update.py — brings an existing flares.db up to date straight from the sources.

The installed package is a snapshot, so this lets a user catch up on their own. It only looks
at what the database already holds: each source is asked for flares newer than the newest one
it has, never for its whole history again.

    LMSAL  scrapes the archive snapshots dated from a week before its newest flare; new
           flares are added (LMSAL sometimes corrects an entry, hence the week of overlap).
    NOAA   downloads the daily event reports from the newest flare's day to yesterday and
           replaces the rows of those days.
    NCEI   downloads the current science-quality report (one file, republished whole) and
           replaces its rows, unless it looks incomplete.

A failure in one source leaves that source untouched and does not stop the others.
"""

import argparse
import ftplib
import io
import os
import re
import sqlite3
import tarfile
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from .db import get_db_path, validate_database
from .exceptions import FlareDatabaseError
from .setup import _lmsal_rows, _load_ncei_flares, _load_noaa_flares

SOURCES = ("LMSAL", "NOAA", "NCEI")

LMSAL_BASE_URL = "https://www.lmsal.com/solarsoft"
LMSAL_INDEX_URL = "https://www.lmsal.com/solarsoft/latest_events_archive.html"
LMSAL_OVERLAP_DAYS = 7

NOAA_FTP_HOST = "ftp.swpc.noaa.gov"
NOAA_FTP_DIR = "pub/indices/events"    # one file per day, only the recent ones
NOAA_WAREHOUSE_DIR = "pub/warehouse"   # <year>/<year>_events.tar.gz for older days

NCEI_LISTING_URL = ("https://data.ngdc.noaa.gov/platforms/solar-space-observing-satellites/goes/multi/"
                    "l2/data/xrsf-l2-flrpt_science/csv/")
NCEI_NAME_RE = re.compile(r"sci_xrsf-l2-flrpt_geo_s\d{8}_e(\d{8})_v[\d-]+\.csv")
NCEI_MAX_LOSS = 0.01   # a new report may not lose more than 1% of its region numbers


def _newest(conn, source):
    value = conn.execute("SELECT MAX(begin_time) FROM flares WHERE source = ?", (source,)).fetchone()[0]
    return datetime.fromisoformat(value) if value else None


def _row_tuple(r):
    return (r["source"], r["begin_time"], r["peak_time"], r["end_time"],
            r["goes_class"], r["goes_class"][0], r["goes_magnitude"], r["active_region"])


def _insert(conn, rows):
    conn.executemany(
        "INSERT INTO flares (source, begin_time, peak_time, end_time, "
        "goes_class, goes_family, goes_magnitude, active_region) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [_row_tuple(r) for r in rows])


def _need_rows(source, newest):
    if newest is None:
        raise FlareDatabaseError(
            f"The database has no {source} flares to continue from. "
            f"Rebuild it with flare-record-build, then update.")


# ---- LMSAL -------------------------------------------------------------------------------

def _parse_lmsal_snapshot(html):
    """The events of one archive snapshot, shaped like events.json entries; None if the page has no event table."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    # the event table has gev_ names and a "Derived Position..." column (named differently over the
    # years); note tables may quote a row, so take the matching table with the most gev_ rows
    table, most = None, 0
    for candidate in soup.find_all("table"):
        cells = [c.get_text(" ", strip=True) for c in candidate.find_all(["th", "td"])]
        count = sum(c.startswith("gev_") for c in cells)
        if count > most and any(c.startswith("Derived Position") for c in cells):
            table, most = candidate, count
    if table is None:
        return None

    texts = [c.get_text(strip=True) for c in table.find_all("td")]
    events, i = [], 0
    while i < len(texts):
        if texts[i].startswith("gev_") and i + 5 < len(texts):
            events.append({"event_id": texts[i], "event_start": texts[i + 1], "event_stop": texts[i + 2],
                           "event_peak": texts[i + 3], "event_GOES": texts[i + 4],
                           "event_position": texts[i + 5]})
            i += 6
        else:
            i += 1
    return events


def _fetch_lmsal(cutoff):
    """Every event in the archive snapshots dated on or after cutoff (YYYYMMDD)."""
    import httpx
    from bs4 import BeautifulSoup

    index = httpx.get(LMSAL_INDEX_URL, timeout=30, follow_redirects=True)
    index.raise_for_status()
    urls = []
    for a in BeautifulSoup(index.text, "html.parser").find_all("a"):
        href = a.get("href")
        if href and "last_events_" in href and href.split("last_events_")[1][:8] >= cutoff:
            urls.append(LMSAL_BASE_URL + "/" + href)

    print(f"  {len(urls)} LMSAL snapshots since {cutoff}")
    events, failed = {}, 0
    for n, url in enumerate(urls, 1):
        if n % 50 == 0:
            print(f"  {n}/{len(urls)} snapshots")
        try:
            response = httpx.get(url, timeout=30, follow_redirects=True)
            response.raise_for_status()
        except Exception:
            failed += 1
            continue
        for event in _parse_lmsal_snapshot(response.text) or []:
            events.setdefault(event["event_id"], event)
    if urls and failed == len(urls):
        raise FlareDatabaseError("none of the LMSAL snapshots could be downloaded")
    if failed:
        print(f"  {failed} snapshots could not be downloaded; they are retried on the next update")
    return list(events.values())


def _update_lmsal(conn):
    newest = _newest(conn, "LMSAL")
    _need_rows("LMSAL", newest)
    cutoff = newest - timedelta(days=LMSAL_OVERLAP_DAYS)
    rows = [r for r in _lmsal_rows(_fetch_lmsal(cutoff.strftime("%Y%m%d")))
            if r["begin_time"] >= cutoff.isoformat()]

    have = {tuple(r) for r in conn.execute(
        "SELECT source, begin_time, peak_time, end_time, goes_class, goes_family, goes_magnitude, active_region "
        "FROM flares WHERE source = 'LMSAL' AND begin_time >= ?", (cutoff.isoformat(),))}
    new = [r for r in {_row_tuple(r): r for r in rows}.values() if _row_tuple(r) not in have]
    _insert(conn, new)
    return len(new)


# ---- NOAA --------------------------------------------------------------------------------

def _fetch_noaa_year(ftp, year, wanted, directory):
    archive = io.BytesIO()
    ftp.retrbinary(f"RETR /{NOAA_WAREHOUSE_DIR}/{year}/{year}_events.tar.gz", archive.write)
    archive.seek(0)
    got = set()
    with tarfile.open(fileobj=archive, mode="r:gz") as tar:
        for member in tar.getmembers():
            day = os.path.basename(member.name).replace("events.txt", "")
            if member.isfile() and day in wanted:
                with open(os.path.join(directory, f"{day}events.txt"), "wb") as f:
                    f.write(tar.extractfile(member).read())
                got.add(day)
    return got


def _fetch_noaa(first, last, directory):
    """Downloads NOAA's daily reports for first..last into directory.
    Returns (days written as YYYYMMDD, a message if it had to stop early else None)."""
    days = [(first + timedelta(n)).strftime("%Y%m%d") for n in range((last - first).days + 1)]
    fetched, problem = set(), None

    ftp = ftplib.FTP(NOAA_FTP_HOST, timeout=60)
    ftp.login()
    try:
        daily = {os.path.basename(p) for p in ftp.nlst(NOAA_FTP_DIR)}
        by_year = {}
        for day in days:
            if f"{day}events.txt" not in daily:
                by_year.setdefault(day[:4], set()).add(day)

        for year, wanted in sorted(by_year.items()):
            try:
                fetched |= _fetch_noaa_year(ftp, year, wanted, directory)
            except ftplib.error_perm:
                continue   # no archive for this year yet
            except Exception as e:
                problem = problem or f"stopped at the {year} archive ({e})"
                break

        ftp.cwd("/" + NOAA_FTP_DIR)
        for day in days:
            name = f"{day}events.txt"
            if name not in daily:
                continue
            part = os.path.join(directory, name + ".part")
            try:
                with open(part, "wb") as f:
                    ftp.retrbinary(f"RETR {name}", f.write)
            except Exception as e:
                if os.path.exists(part):
                    os.remove(part)
                problem = problem or f"stopped at {day} ({e})"
                break
            os.replace(part, os.path.join(directory, name))
            fetched.add(day)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
    return fetched, problem


def _update_noaa(conn):
    newest = _newest(conn, "NOAA")
    _need_rows("NOAA", newest)
    first, last = newest.date(), date.today() - timedelta(days=1)
    if first > last:
        return 0

    with tempfile.TemporaryDirectory() as directory:
        fetched, problem = _fetch_noaa(first, last, directory)
        rows = _load_noaa_flares(directory)
    if problem:
        print(f"  NOAA {problem}; the days after it are retried on the next update")

    # a day's report is complete, so it replaces whatever the database had for that day
    removed = 0
    for day in sorted(fetched):
        removed += conn.execute("DELETE FROM flares WHERE source = 'NOAA' AND substr(begin_time, 1, 10) = ?",
                                (f"{day[:4]}-{day[4:6]}-{day[6:]}",)).rowcount
    rows = list({_row_tuple(r): r for r in rows}.values())
    _insert(conn, rows)
    return len(rows) - removed


# ---- NCEI --------------------------------------------------------------------------------

def _fetch_ncei(directory):
    import httpx
    listing = httpx.get(NCEI_LISTING_URL, timeout=60, follow_redirects=True)
    listing.raise_for_status()
    names = set(m.group(0) for m in NCEI_NAME_RE.finditer(listing.text))
    if not names:
        raise FlareDatabaseError("no SciQuality report found in the NCEI listing")
    name = max(names, key=lambda n: NCEI_NAME_RE.fullmatch(n).group(1))
    path = os.path.join(directory, name)
    print(f"  downloading {name}")
    with httpx.stream("GET", NCEI_LISTING_URL + name, timeout=300, follow_redirects=True) as response:
        response.raise_for_status()
        with open(path, "wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    return path


def _update_ncei(conn):
    with tempfile.TemporaryDirectory() as directory:
        rows = _load_ncei_flares(_fetch_ncei(directory))

    have, have_regions = conn.execute(
        "SELECT COUNT(*), COUNT(active_region) FROM flares WHERE source = 'NCEI'").fetchone()
    regions = sum(1 for r in rows if r["active_region"] is not None)
    # NCEI has published reports that kept every row but blanked columns: don't swap those in
    if len(rows) < have or regions < have_regions * (1 - NCEI_MAX_LOSS):
        raise FlareDatabaseError(
            f"the new NCEI report has {len(rows)} flares ({regions} with a region) against the "
            f"{have} ({have_regions}) already stored, so it looks incomplete; keeping what you have")

    conn.execute("DELETE FROM flares WHERE source = 'NCEI'")
    _insert(conn, rows)
    return len(rows) - have


# ---- entry points ------------------------------------------------------------------------

_UPDATERS = {"LMSAL": _update_lmsal, "NOAA": _update_noaa, "NCEI": _update_ncei}


def _check_sources(sources):
    if sources is None:
        return list(SOURCES)
    names = [sources] if isinstance(sources, str) else list(sources) if isinstance(sources, (list, tuple, set)) else []
    cleaned = [s.strip().upper() if isinstance(s, str) else s for s in names]
    if not cleaned or any(s not in SOURCES for s in cleaned):
        raise ValueError(f"sources must be some of {list(SOURCES)}, got {sources!r}")
    return list(dict.fromkeys(cleaned))


def update_database(sources=None, output_path=None):
    """Adds the flares published since the database was last updated.

    sources: "LMSAL", "NOAA", "NCEI" or a list of them; omit for all three.
    output_path: the database to update (defaults to the one getData() reads).

    Returns {source: {"added": n}} or {source: {"error": message}}; "added" is the net change
    in that source's rows. A source that fails is left as it was and the others still run.
    Needs an existing database (flare-record-build) to continue from.
    """
    names = _check_sources(sources)
    path = Path(output_path) if output_path else get_db_path()
    validate_database(path)

    results = {}
    conn = sqlite3.connect(str(path))
    try:
        for name in names:
            print(f"Updating {name}...")
            try:
                with conn:   # all of this source's changes, or none of them
                    results[name] = {"added": _UPDATERS[name](conn)}
            except Exception as e:
                results[name] = {"error": str(e) or type(e).__name__}
    finally:
        conn.close()
    return results


def _update_cli(argv=None):
    # Entry point for the `flare-record-update` command.
    parser = argparse.ArgumentParser(
        prog="flare-record-update",
        description="Bring your flare database up to date from LMSAL, NOAA and NCEI.")
    parser.add_argument("--source", nargs="+", metavar="SOURCE", type=str.upper, choices=SOURCES,
                        help="which sources to update (default: all): lmsal noaa ncei")
    args = parser.parse_args(argv)

    try:
        results = update_database(args.source)
    except FlareDatabaseError as e:
        raise SystemExit(str(e))

    print()
    for name, result in results.items():
        print(f"{name}: " + (f"{result['added']:+d} flares" if "error" not in result else f"FAILED, {result['error']}"))
    if any("error" in r for r in results.values()):
        raise SystemExit(1)
