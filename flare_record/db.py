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
import sqlite3
from pathlib import Path


from .exceptions import FlareDatabaseInvalidError, FlareDatabaseNotFoundError

DEFAULT_DB_PATH = Path.home() / ".flare_record" / "flares.db"
REQUIRED_COLUMNS = {"source", "begin_time", "peak_time", "end_time",
                    "goes_class", "goes_family", "goes_magnitude", "active_region"}

def get_db_path(): 
    override = os.environ.get("FLARE_RECORD_DB")
    return Path(override).expanduser() if override else DEFAULT_DB_PATH

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
    validate_database(path)
    return sqlite3.connect(str(path))