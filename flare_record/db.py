"""
db.py — finds the local database and checks it's actually usable before
any query touches it.
 
Design choice: the database is a single SQLite file, not the raw
events.json/noaa_data files. SQLite lets getData() filter with an
indexed where clause instead of scanning every record in Python on
every call, and it's one file, which makes "download the db" a
single-file operation.
"""

import os 
import shutil
import sqlite3
from pathlib import Path


from .exceptions import FlareDatabaseInvalidError, FlareDatabaseNotFoundError

DEFAULT_DB_PATH = Path.home() / ".flare_record" / "flares.db"
# a snapshot of the database that ships inside the package, so there is something to query and update at once
BUNDLED_DB_PATH = Path(__file__).parent / "data" / "flares.db"
REQUIRED_COLUMNS = {"source", "begin_time", "peak_time", "end_time",
                    "goes_class", "goes_family", "goes_magnitude", "active_region"}

def get_db_path(): 
    override = os.environ.get("FLARE_RECORD_DB")
    return Path(override).expanduser() if override else DEFAULT_DB_PATH

def install_bundled_database(path):
    """First use: copies the database that ships with the package to the default location.

    Does nothing when the user already has a database there (an upgrade never overwrites it, so
    their updates are kept) or points FLARE_RECORD_DB somewhere else. It is a copy, not a link,
    because the install folder is often read-only and updates go into the user's own file."""
    if path != DEFAULT_DB_PATH or path.exists() or not BUNDLED_DB_PATH.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Copying the database that ships with flare_record to {path} (update it with flare-record-update)")
    part = path.with_name(path.name + ".part")   # a half-copied file must never look like a database
    shutil.copyfile(BUNDLED_DB_PATH, part)
    os.replace(part, path)

def validate_database(path):
    if not path.exists(): 
        raise FlareDatabaseNotFoundError(path)
    if not path.is_file():
        raise FlareDatabaseInvalidError(path, "it is a directory, not a database file")
    if path.stat().st_size == 0:
        raise FlareDatabaseInvalidError(path, "the file is empty (0 bytes), possibly an interrupted build or download")
    
    conn = None
    try: 
        conn = sqlite3.connect(str(path))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='flares'"
        )
        if cursor.fetchone() is None: 
            raise FlareDatabaseInvalidError(path, "missing the 'flares' table")
        
        cursor = conn.execute("PRAGMA table_info(flares)")
        actual_columns = {row[1] for row in cursor.fetchall()}
        missing = REQUIRED_COLUMNS - actual_columns
        if missing: 
            raise FlareDatabaseInvalidError(path, f"missing columns: {sorted(missing)}")
        row_count = conn.execute("SELECT COUNT(*) FROM flares").fetchone()[0]
        if row_count == 0:
            raise FlareDatabaseInvalidError(path, "the flares table is empty")
        
    except sqlite3.DatabaseError as e:
        # This is what a corrupted file raises, e.g. a partial download, or a non-database file that happens to sit at this path.
        raise FlareDatabaseInvalidError(path, f"not a valid SQLite file ({e})")
    finally:
        if conn is not None:
            conn.close()

def connect():
    path = get_db_path()
    install_bundled_database(path)
    validate_database(path)
    return sqlite3.connect(str(path))