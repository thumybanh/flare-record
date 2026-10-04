"""
Tests for getData() and the database checks.

Most tests build a tiny database from made-up LMSAL/NOAA files written in the
same formats as the real ones, so they run anywhere. The tests at the bottom
check known flares in the real database and are skipped if it hasn't been built.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

import flare_record as fs
from flare_record.db import DEFAULT_DB_PATH
from flare_record.setup import build_database


LMSAL_EVENTS = [
    {"event_start": "2024/05/14 02:03:00", "event_peak": "02:09:00", "event_stop": "02:19:00",
     "event_GOES": "X1.7", "event_position": "S18W45( 3664 )"},
    {"event_start": "2024/05/14 12:40:00", "event_peak": "12:55:00", "event_stop": "13:05:00",
     "event_GOES": "X1.2", "event_position": "S18W50( 3664 )"},
    # Solar-minimum LMSAL entries write the class in lowercase.
    {"event_start": "2009/10/23 13:19:00", "event_peak": "13:25:00", "event_stop": "13:30:00",
     "event_GOES": "a3.7", "event_position": ""},
    # Last minute of May, then first minute of June, to check month boundaries.
    {"event_start": "2024/05/31 23:59:00", "event_peak": "00:05:00", "event_stop": "00:10:00",
     "event_GOES": "C2.0", "event_position": "N10E10( 3697 )"},
    {"event_start": "2024/06/01 00:00:00", "event_peak": "00:04:00", "event_stop": "00:09:00",
     "event_GOES": "C3.0", "event_position": "N10E09( 3697 )"},
    # No usable class, so it must be skipped.
    {"event_start": "2024/05/20 10:00:00", "event_peak": "10:05:00", "event_stop": "10:10:00",
     "event_GOES": "", "event_position": ""},
]

NOAA_FILES = {
    "20240514events.txt": """:Product: 20240514events.txt
#Event    Begin    Max       End  Obs  Q  Type  Loc/Frq   Particulars       Reg#
2890      B0000   ////     B0608  LEA  2   LPS  ////      0.14              3664
3000 +     0203   0209      0219  G16  5   XRA  1-8A      X1.7    1.4E-01   3664
3010       1240   1255      1305  G16  5   XRA  1-8A      X1.2    1.1E-01   3664
3010       1240   1255      1305  G16  5   XRA  1-8A      X1.2    1.1E-01   3664
8140      A1903   1939     A2028  G16  5   XRA  1-8A      M7.3    2.7E-01   3697
""",
    "20000711events.txt": """:Product: 20000711events.txt
8990 +     1212   1310      1335  GO8  5   XRA  1-8A      X1      3.1E-01   9077
""",
    "20010102events.txt": """:Product: 20010102events.txt
1000       1000   1010      1020  GO8  5   XRA  1-8A      C1.23   2.0E-03   9300
1010       1100   1110      1120  GO8  5   XRA  1-8A      C6,2    1.4E-02   9301
""",
    "19991221events.txt": """:Product: 19991221events.txt
2000       0236   0240      0250  GO8  5   XRA  1-8A      C
""",
    # Not a dated events file, so the build must ignore it.
    "notes.txt": "3000 +     0203   0209      0219  G16  5   XRA  1-8A      X9.9\n",
}


@pytest.fixture(scope="module")
def sample_db(tmp_path_factory):
    folder = tmp_path_factory.mktemp("flares")
    events_json = folder / "events.json"
    events_json.write_text(json.dumps(LMSAL_EVENTS))
    noaa_dir = folder / "noaa_data"
    noaa_dir.mkdir()
    for name, text in NOAA_FILES.items():
        (noaa_dir / name).write_text(text)
    return build_database(events_json, noaa_dir, folder / "flares.db")


@pytest.fixture(autouse=True)
def use_sample_db(request, sample_db, monkeypatch):
    if "real_db" not in request.keywords:
        monkeypatch.setenv("FLARE_RECORD_DB", str(sample_db))


def begins(results):
    return [(r["source"], r["begin_time"], r["goes_class"]) for r in results]


# --- building the database --------------------------------------------------

def test_build_keeps_every_valid_row_once():
    # 5 LMSAL with a class, 7 NOAA XRA rows (one exact duplicate dropped, notes.txt ignored).
    assert len(fs.getData(source="LMSAL")) == 5
    assert len(fs.getData(source="NOAA")) == 7


def test_build_keeps_noaa_times_with_qualifier_letters():
    (row,) = fs.getData(GOESclass="M7.3")
    assert (row["begin_time"], row["peak_time"], row["end_time"]) == (
        "2024-05-14T19:03:00", "2024-05-14T19:39:00", "2024-05-14T20:28:00")


def test_build_normalizes_old_class_formats():
    classes = {r["goes_class"] for r in fs.getData(source="NOAA")}
    assert {"X1.0", "C1.23", "C6.2", "C"} <= classes
    assert "X1" not in classes


def test_build_uppercases_lmsal_classes():
    assert begins(fs.getData(timerange="20091023")) == [("LMSAL", "2009-10-23T13:19:00", "A3.7")]


def test_build_rolls_peak_and_end_past_midnight():
    (row,) = fs.getData(timerange="20240531")
    assert row["peak_time"] == "2024-06-01T00:05:00"
    assert row["end_time"] == "2024-06-01T00:10:00"


def test_build_reads_active_regions_from_both_sources():
    assert {r["active_region"] for r in fs.getData(GOESclass="X1.7")} == {3664}
    assert fs.getData(timerange="20091023")[0]["active_region"] is None


# --- source -----------------------------------------------------------------

@pytest.mark.parametrize("source", ["NOAA", "noaa", " NOAA ", ["noaa"], ("NOAA",)])
def test_source_single(source):
    assert {r["source"] for r in fs.getData(source=source)} == {"NOAA"}


def test_source_list_of_both_equals_no_filter():
    assert fs.getData(source=["NOAA", "LMSAL"]) == fs.getData()


@pytest.mark.parametrize("source", ["GOES", "", [], ["NOAA", "GOES"], 123])
def test_source_invalid(source):
    with pytest.raises(ValueError, match="source must be"):
        fs.getData(source=source)


# --- timerange --------------------------------------------------------------

@pytest.mark.parametrize("timerange", [
    "20240514", "2024-05-14", "2024/05/14", " 20240514 ",
    date(2024, 5, 14), ("20240514", "2024-05-14"), ["20240514", "20240514"],
])
def test_timerange_single_day_forms_agree(timerange):
    assert len(fs.getData(timerange=timerange, source="NOAA")) == 3


def test_timerange_whole_month_includes_last_minute():
    may = begins(fs.getData(timerange="202405"))
    assert ("LMSAL", "2024-05-31T23:59:00", "C2.0") in may
    assert all(b.startswith("2024-05") for _, b, _ in may)
    assert fs.getData(timerange="2024-05") == fs.getData(timerange="202405")


def test_timerange_whole_year():
    assert len(fs.getData(timerange="2024")) == 7


def test_timerange_pair_runs_from_start_of_first_to_end_of_second():
    assert len(fs.getData(timerange=("202405", "202406"))) == 7


def test_timerange_datetime_window_is_exact():
    window = (datetime(2024, 5, 14, 12), datetime(2024, 5, 14, 18))
    assert {r["goes_class"] for r in fs.getData(timerange=window)} == {"X1.2"}


def test_timerange_timezone_aware_datetime_converted_to_utc():
    pacific = timezone(timedelta(hours=-7))
    window = (datetime(2024, 5, 14, 5, tzinfo=pacific), datetime(2024, 5, 14, 11, tzinfo=pacific))
    assert {r["goes_class"] for r in fs.getData(timerange=window)} == {"X1.2"}


def test_timerange_filters_on_begin_time_only():
    # C2.0 peaks on June 1 but began on May 31, so June doesn't include it.
    assert "C2.0" not in {r["goes_class"] for r in fs.getData(timerange="20240601")}


def test_timerange_outside_data_is_empty():
    assert fs.getData(timerange="1900") == []


@pytest.mark.parametrize("timerange", ["20240231", "20241301", "2024-13", "00000101"])
def test_timerange_impossible_date(timerange):
    with pytest.raises(ValueError, match="not a real date"):
        fs.getData(timerange=timerange)


@pytest.mark.parametrize("timerange", [
    "2024-5-14", "24", "202", "20240514 1200", "May 2024", "", 20240514, 2024.0, True,
])
def test_timerange_bad_format(timerange):
    with pytest.raises(ValueError, match="timerange dates must look like"):
        fs.getData(timerange=timerange)


@pytest.mark.parametrize("timerange", [(), ("20240501",), ("20240501", "20240510", "20240515")])
def test_timerange_wrong_number_of_items(timerange):
    with pytest.raises(ValueError, match="one date or a"):
        fs.getData(timerange=timerange)


@pytest.mark.parametrize("timerange", [("20240515", "20240501"), ("2025", "2024")])
def test_timerange_reversed(timerange):
    with pytest.raises(ValueError, match="is after end"):
        fs.getData(timerange=timerange)


# --- flareType --------------------------------------------------------------

@pytest.mark.parametrize("flareType", ["x", "X", " x "])
def test_flare_type_family(flareType):
    assert {r["goes_class"] for r in fs.getData(flareType=flareType)} == {"X1.7", "X1.2", "X1.0"}


@pytest.mark.parametrize("flareType", ["Q", "", "X1.0", ["M", "X"], 5])
def test_flare_type_invalid(flareType):
    with pytest.raises(ValueError, match="flareType must be"):
        fs.getData(flareType=flareType)


# --- GOESclass --------------------------------------------------------------

def test_goes_class_bin_without_decimal():
    assert {r["goes_class"] for r in fs.getData(GOESclass="x1")} == {"X1.7", "X1.2", "X1.0"}


@pytest.mark.parametrize("goes_class", ["X1.0", "x1.0", " X1.0 ", "X1,0", "X1.00"])
def test_goes_class_exact_matches_old_noaa_x1(goes_class):
    assert begins(fs.getData(GOESclass=goes_class)) == [("NOAA", "2000-07-11T12:12:00", "X1.0")]


def test_goes_class_matches_at_the_precision_typed():
    assert {r["goes_class"] for r in fs.getData(GOESclass="C1.2")} == {"C1.23"}
    assert {r["goes_class"] for r in fs.getData(GOESclass="C1.23")} == {"C1.23"}
    assert fs.getData(GOESclass="C1.24") == []


def test_goes_class_family_letter_includes_rows_without_magnitude():
    assert "C" in {r["goes_class"] for r in fs.getData(GOESclass="C")}
    assert "C" not in {r["goes_class"] for r in fs.getData(GOESclass="C1")}


@pytest.mark.parametrize("goes_class", ["Z9.9", "X1.0.0", "", "1.0", 1.0, ["X1"]])
def test_goes_class_invalid(goes_class):
    with pytest.raises(ValueError, match="GOESclass must"):
        fs.getData(GOESclass=goes_class)


# --- activeRegion -----------------------------------------------------------

@pytest.mark.parametrize("region", [13664, 3664, "13664", "3664", "AR3664", "ar 13664", " AR13664 "])
def test_active_region_forms_agree(region):
    results = fs.getData(activeRegion=region, flareType="X")
    assert {(r["source"], r["goes_class"]) for r in results} == {
        ("NOAA", "X1.7"), ("NOAA", "X1.2"), ("LMSAL", "X1.7"), ("LMSAL", "X1.2")}


def test_active_region_unknown_is_empty():
    assert fs.getData(activeRegion=99999) == []


@pytest.mark.parametrize("region", [0, 10000, -5, "abc", "", True, 3664.0, [3664]])
def test_active_region_invalid(region):
    with pytest.raises(ValueError, match="activeRegion must be"):
        fs.getData(activeRegion=region)


# --- combining filters --------------------------------------------------------

def test_class_bin_on_a_day_from_one_source():
    assert begins(fs.getData(GOESclass="X1", timerange="20240514", source="NOAA")) == [
        ("NOAA", "2024-05-14T02:03:00", "X1.7"), ("NOAA", "2024-05-14T12:40:00", "X1.2")]


def test_class_bin_on_a_day_from_both_sources_sorted_by_time():
    results = fs.getData(GOESclass="X1", timerange="20240514")
    assert len(results) == 4
    assert [r["begin_time"] for r in results] == sorted(r["begin_time"] for r in results)


def test_same_flare_from_both_sources_by_region_and_class():
    assert {r["source"] for r in fs.getData(activeRegion=3664, GOESclass="X1.7")} == {"NOAA", "LMSAL"}


def test_conflicting_filters_return_empty_not_error():
    assert fs.getData(flareType="M", GOESclass="X1") == []
    assert fs.getData(activeRegion=9077, source="LMSAL") == []


def test_positional_arguments():
    assert fs.getData("NOAA", "20000711") == fs.getData(source="NOAA", timerange="20000711")


# --- database problems --------------------------------------------------------

def make_db(path, sql):
    conn = sqlite3.connect(str(path))
    conn.executescript(sql)
    conn.close()
    return path


ALL_COLUMNS = "source, begin_time, peak_time, end_time, goes_class, goes_family, goes_magnitude, active_region"


@pytest.mark.parametrize("setup, error, message", [
    (lambda tmp: tmp / "missing.db", fs.FlareDatabaseNotFoundError, "No flare database found"),
    (lambda tmp: tmp, fs.FlareDatabaseInvalidError, "is a directory"),
    (lambda tmp: (tmp / "empty.db").touch() or tmp / "empty.db", fs.FlareDatabaseInvalidError, "0 bytes"),
    (lambda tmp: (tmp / "junk.db").write_text("not a database") and tmp / "junk.db",
     fs.FlareDatabaseInvalidError, "not a valid SQLite file"),
    (lambda tmp: make_db(tmp / "other.db", "CREATE TABLE other(a);"),
     fs.FlareDatabaseInvalidError, "missing the 'flares' table"),
    (lambda tmp: make_db(tmp / "old.db", "CREATE TABLE flares(source, begin_time, peak_time, end_time, "
                                         "goes_class, active_region); INSERT INTO flares VALUES (1,2,3,4,5,6);"),
     fs.FlareDatabaseInvalidError, "missing columns: \\['goes_family', 'goes_magnitude'\\]"),
    (lambda tmp: make_db(tmp / "rows.db", f"CREATE TABLE flares({ALL_COLUMNS});"),
     fs.FlareDatabaseInvalidError, "the flares table is empty"),
])
def test_database_problems_raise_clear_errors(tmp_path, monkeypatch, setup, error, message):
    monkeypatch.setenv("FLARE_RECORD_DB", str(setup(tmp_path)))
    with pytest.raises(error, match=message):
        fs.getData()


def test_database_errors_share_a_base_class(tmp_path, monkeypatch):
    monkeypatch.setenv("FLARE_RECORD_DB", str(tmp_path / "missing.db"))
    with pytest.raises(fs.FlareDatabaseError):
        fs.getData()


def test_database_path_expands_home(sample_db, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(sample_db.parent))
    monkeypatch.setenv("FLARE_RECORD_DB", "~/flares.db")
    assert len(fs.getData()) == 12


def test_empty_database_env_var_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("FLARE_RECORD_DB", "")
    assert fs.db.get_db_path() == DEFAULT_DB_PATH


# --- real database (skipped until flare-record-build has been run) -----------

real_db = pytest.mark.skipif(not DEFAULT_DB_PATH.is_file(), reason="real database not built")


@pytest.fixture
def real(monkeypatch):
    monkeypatch.delenv("FLARE_RECORD_DB", raising=False)


@real_db
@pytest.mark.real_db
def test_real_old_noaa_x1_matches_x1_0(real):
    assert "2000-07-11T12:12:00" in {r["begin_time"] for r in fs.getData(GOESclass="X1.0", source="NOAA")}


@real_db
@pytest.mark.real_db
def test_real_region_13664_in_both_sources(real):
    assert {r["source"] for r in fs.getData(activeRegion=13664, flareType="X")} == {"NOAA", "LMSAL"}


@real_db
@pytest.mark.real_db
def test_real_database_has_no_exact_duplicates(real):
    rows = [tuple(r.values()) for r in fs.getData()]
    assert len(rows) == len(set(rows))
