"""
Tests for update_database() and flare-record-update.

Nothing here touches the network: the three fetch functions are replaced by fakes that hand back
what LMSAL, NOAA and NCEI would have published, so the tests check what is done with it.
"""

import json
import sqlite3
from datetime import datetime

import pytest

import flare_record as fs
from flare_record import update
from flare_record.exceptions import FlareDatabaseNotFoundError
from flare_record.setup import build_database

NCEI_HEADER = "time,start_time,end_time,flare_id,xrsb_irrad,flare_class,active_region\n"


def lmsal(start, peak, stop, cls, position="N10E10( 3664 )"):
    return {"event_start": start, "event_peak": peak, "event_stop": stop,
            "event_GOES": cls, "event_position": position}


def noaa_line(begin, peak, end, cls, region):
    return f"3000       {begin}   {peak}      {end}  G16  5   XRA  1-8A      {cls}    1.4E-01   {region}\n"


@pytest.fixture
def db(tmp_path, monkeypatch):
    events = tmp_path / "events.json"
    events.write_text(json.dumps([
        lmsal("2024/05/14 02:03:00", "02:09:00", "02:19:00", "X1.7"),
        lmsal("2024/05/14 12:40:00", "12:55:00", "13:05:00", "X1.2"),
    ]))
    noaa = tmp_path / "noaa"
    noaa.mkdir()
    (noaa / "20240514events.txt").write_text(noaa_line("0203", "0209", "0219", "X1.7", 3664))
    ncei = tmp_path / "ncei.csv"
    ncei.write_text(NCEI_HEADER +
                    "2024-05-14 02:09:00,2024-05-14 02:03:00,2024-05-14 02:19:00,1,1e-4,X1.7,3664\n")
    path = build_database(events, noaa, tmp_path / "flares.db", ncei_csv_path=ncei)
    monkeypatch.setenv("FLARE_RECORD_DB", str(path))
    return path


def rows(db, source):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT begin_time, goes_class, active_region FROM flares "
                            "WHERE source = ? ORDER BY begin_time", (source,)).fetchall()
    finally:
        conn.close()


# --- building with NCEI ------------------------------------------------------

def test_ncei_rows_are_a_source_of_their_own(db):
    assert fs.getData(source="ncei") == [{
        "source": "NCEI", "begin_time": "2024-05-14T02:03:00", "peak_time": "2024-05-14T02:09:00",
        "end_time": "2024-05-14T02:19:00", "goes_class": "X1.7", "active_region": 3664}]


# --- LMSAL -------------------------------------------------------------------

def test_lmsal_adds_only_flares_it_does_not_have(db, monkeypatch):
    asked = []

    def fake(cutoff):
        asked.append(cutoff)
        return [dict(lmsal("2024/05/14 12:40:00", "12:55:00", "13:05:00", "X1.2"), event_id="a"),   # have it
                dict(lmsal("2024/05/15 08:00:00", "08:10:00", "08:30:00", "M2.0"), event_id="b"),   # new
                dict(lmsal("2024/05/01 08:00:00", "08:10:00", "08:30:00", "C1.0"), event_id="c")]   # before the window
    monkeypatch.setattr(update, "_fetch_lmsal", fake)

    assert update.update_database("lmsal") == {"LMSAL": {"added": 1}}
    assert asked == ["20240507"]   # a week before the newest LMSAL flare (2024-05-14 12:40)
    assert [r[:2] for r in rows(db, "LMSAL")] == [
        ("2024-05-14T02:03:00", "X1.7"), ("2024-05-14T12:40:00", "X1.2"), ("2024-05-15T08:00:00", "M2.0")]

    assert update.update_database("lmsal") == {"LMSAL": {"added": 0}}   # again: nothing new


def test_parse_lmsal_snapshot_reads_the_event_table_and_skips_note_tables():
    html = """<table><tr><td>gev_20240514_0203 quoted in a note</td></tr></table>
    <table><tr><th>Event</th><th>Start</th><th>Stop</th><th>Peak</th><th>GOES</th><th>Derived Position</th></tr>
    <tr><td>gev_20240514_0203</td><td>2024/05/14 02:03:00</td><td>02:19:00</td><td>02:09:00</td><td>X1.7</td>
        <td>N13E15( 3664 )</td></tr>
    <tr><td>gev_20240514_1240</td><td>2024/05/14 12:40:00</td><td>13:05:00</td><td>12:55:00</td><td>X1.2</td>
        <td>N13E10( 3664 )</td></tr></table>"""
    events = update._parse_lmsal_snapshot(html)
    assert [e["event_id"] for e in events] == ["gev_20240514_0203", "gev_20240514_1240"]
    assert events[0] == {"event_id": "gev_20240514_0203", "event_start": "2024/05/14 02:03:00",
                         "event_stop": "02:19:00", "event_peak": "02:09:00", "event_GOES": "X1.7",
                         "event_position": "N13E15( 3664 )"}
    assert update._parse_lmsal_snapshot("<html><table><tr><td>nothing</td></tr></table></html>") is None


# --- NOAA --------------------------------------------------------------------

def test_noaa_replaces_the_days_it_fetched_and_adds_new_ones(db, monkeypatch):
    def fake(first, last, directory):
        assert first == datetime(2024, 5, 14).date()   # starts on the day of the newest flare
        # the 14th now has a second flare; the 15th is new
        with open(f"{directory}/20240514events.txt", "w") as f:
            f.write(noaa_line("0203", "0209", "0219", "X1.7", 3664) + noaa_line("1240", "1255", "1305", "X1.2", 3664))
        with open(f"{directory}/20240515events.txt", "w") as f:
            f.write(noaa_line("0800", "0810", "0830", "M2.0", 3664))
        return {"20240514", "20240515"}, None
    monkeypatch.setattr(update, "_fetch_noaa", fake)

    assert update.update_database("NOAA") == {"NOAA": {"added": 2}}
    assert [r[:2] for r in rows(db, "NOAA")] == [
        ("2024-05-14T02:03:00", "X1.7"), ("2024-05-14T12:40:00", "X1.2"), ("2024-05-15T08:00:00", "M2.0")]


def test_noaa_day_with_no_report_keeps_its_rows(db, monkeypatch):
    monkeypatch.setattr(update, "_fetch_noaa", lambda first, last, directory: (set(), "stopped at 20240514 (timed out)"))
    assert update.update_database("NOAA") == {"NOAA": {"added": 0}}
    assert len(rows(db, "NOAA")) == 1


# --- NCEI --------------------------------------------------------------------

def fake_ncei(text):
    def fetch(directory):
        path = f"{directory}/report.csv"
        with open(path, "w") as f:
            f.write(NCEI_HEADER + text)
        return path
    return fetch


NEW_NCEI = ("2024-05-14 02:09:00,2024-05-14 02:03:00,2024-05-14 02:19:00,1,1e-4,X1.7,3664\n"
            "2024-05-15 08:10:00,2024-05-15 08:00:00,2024-05-15 08:30:00,2,2e-5,M2.0,3664\n")


def test_ncei_report_replaces_its_rows(db, monkeypatch):
    monkeypatch.setattr(update, "_fetch_ncei", fake_ncei(NEW_NCEI))
    assert update.update_database("NCEI") == {"NCEI": {"added": 1}}
    assert [r[:2] for r in rows(db, "NCEI")] == [("2024-05-14T02:03:00", "X1.7"), ("2024-05-15T08:00:00", "M2.0")]


@pytest.mark.parametrize("text", [
    "",                                                                                   # fewer flares than stored
    "2024-05-14 02:09:00,2024-05-14 02:03:00,2024-05-14 02:19:00,1,1e-4,X1.7,\n",         # same flares, region blanked
])
def test_incomplete_ncei_report_is_refused_and_leaves_the_rows(db, monkeypatch, text):
    monkeypatch.setattr(update, "_fetch_ncei", fake_ncei(text))
    result = update.update_database("NCEI")
    assert "looks incomplete" in result["NCEI"]["error"]
    assert rows(db, "NCEI") == [("2024-05-14T02:03:00", "X1.7", 3664)]


# --- all sources, failures, errors ------------------------------------------

def test_one_failing_source_does_not_stop_or_change_the_others(db, monkeypatch):
    def broken(cutoff):
        raise ConnectionError("LMSAL is down")
    monkeypatch.setattr(update, "_fetch_lmsal", broken)
    monkeypatch.setattr(update, "_fetch_noaa", lambda first, last, directory: (set(), None))
    monkeypatch.setattr(update, "_fetch_ncei", fake_ncei(NEW_NCEI))

    result = update.update_database()
    assert result == {"LMSAL": {"error": "LMSAL is down"}, "NOAA": {"added": 0}, "NCEI": {"added": 1}}
    assert len(rows(db, "LMSAL")) == 2


def test_source_with_no_rows_to_continue_from_says_so(tmp_path, monkeypatch):
    events = tmp_path / "events.json"
    events.write_text(json.dumps([lmsal("2024/05/14 02:03:00", "02:09:00", "02:19:00", "X1.7")]))
    noaa = tmp_path / "noaa"
    noaa.mkdir()
    path = build_database(events, noaa, tmp_path / "flares.db")
    monkeypatch.setenv("FLARE_RECORD_DB", str(path))
    result = update.update_database("NOAA")
    assert "no NOAA flares to continue from" in result["NOAA"]["error"]


def test_needs_an_existing_database(tmp_path, monkeypatch):
    monkeypatch.setenv("FLARE_RECORD_DB", str(tmp_path / "missing.db"))
    with pytest.raises(FlareDatabaseNotFoundError):
        update.update_database()


@pytest.mark.parametrize("sources", ["GOES", [], ["noaa", "x"], 5])
def test_invalid_sources(db, sources):
    with pytest.raises(ValueError, match="sources must be"):
        update.update_database(sources)


def test_cli_prints_a_line_per_source_and_exits_1_on_failure(db, monkeypatch, capsys):
    monkeypatch.setattr(update, "_fetch_lmsal", lambda cutoff: [])
    monkeypatch.setattr(update, "_fetch_ncei", fake_ncei(""))
    with pytest.raises(SystemExit) as stopped:
        update._update_cli(["--source", "lmsal", "ncei"])
    assert stopped.value.code == 1
    out = capsys.readouterr().out
    assert "LMSAL: +0 flares" in out and "NCEI: FAILED" in out
