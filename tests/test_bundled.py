"""
Tests for the database that ships inside the package and is copied to the user's own location on first use.
"""

import sqlite3

import pytest

import flare_record as fs
from flare_record import db


def make_database(path, begin):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE flares (source TEXT NOT NULL, begin_time TEXT NOT NULL, peak_time TEXT, end_time TEXT, "
                 "goes_class TEXT NOT NULL, goes_family TEXT NOT NULL, goes_magnitude REAL, active_region INTEGER, position TEXT)")
    conn.execute("INSERT INTO flares VALUES ('NOAA', ?, NULL, NULL, 'X1.0', 'X', 1.0, NULL, NULL)", (begin,))
    conn.commit()
    conn.close()


@pytest.fixture
def places(tmp_path, monkeypatch):
    # a fake package snapshot and a fake home, with no FLARE_RECORD_DB override
    monkeypatch.delenv("FLARE_RECORD_DB", raising=False)
    bundled, home = tmp_path / "package" / "flares.db", tmp_path / "home" / "flares.db"
    monkeypatch.setattr(db, "BUNDLED_DB_PATH", bundled)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", home)
    make_database(bundled, "2026-01-01T00:00:00")
    return bundled, home


def test_first_query_copies_the_snapshot_to_the_users_location(places, capsys):
    bundled, home = places
    assert not home.exists()
    assert [r["begin_time"] for r in fs.getData()] == ["2026-01-01T00:00:00"]
    assert home.exists() and bundled.exists()
    assert "Copying the database that ships with flare_record" in capsys.readouterr().out
    assert [p.name for p in home.parent.iterdir()] == ["flares.db"]   # no .part left behind


def test_an_existing_database_is_never_overwritten(places):
    bundled, home = places
    make_database(home, "2025-06-01T00:00:00")   # the user's own, say after their updates
    assert [r["begin_time"] for r in fs.getData()] == ["2025-06-01T00:00:00"]


def test_update_also_starts_from_the_snapshot(places, monkeypatch):
    from flare_record import update
    bundled, home = places
    monkeypatch.setattr(update, "_fetch_noaa", lambda first, last, directory: (set(), None))
    assert fs.update_database("NOAA") == {"NOAA": {"added": 0}}
    assert home.exists()


def test_an_explicit_path_is_not_replaced_by_the_snapshot(places, tmp_path, monkeypatch):
    monkeypatch.setenv("FLARE_RECORD_DB", str(tmp_path / "mine" / "missing.db"))
    with pytest.raises(fs.FlareDatabaseNotFoundError):
        fs.getData()
    assert not (tmp_path / "mine").exists()


def test_without_a_snapshot_the_missing_database_error_stays(places):
    bundled, home = places
    bundled.unlink()
    with pytest.raises(fs.FlareDatabaseNotFoundError):
        fs.getData()


def test_the_snapshot_in_the_package_is_a_valid_database_with_all_three_sources():
    assert db.BUNDLED_DB_PATH.exists()
    db.validate_database(db.BUNDLED_DB_PATH)
    conn = sqlite3.connect(str(db.BUNDLED_DB_PATH))
    try:
        sources = {row[0] for row in conn.execute("SELECT DISTINCT source FROM flares")}
    finally:
        conn.close()
    assert sources == {"LMSAL", "NOAA", "NCEI"}
