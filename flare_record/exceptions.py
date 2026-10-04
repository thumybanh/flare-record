"""
exceptions.py — errors that tell the user exactly what to do next.
 
A generic FileNotFoundError just says "not found." These say "not found,
and here is the exact command that fixes it," which is the whole point
of raising a custom error.
"""
class FlareDatabaseError(Exception): 
   """Base class for every error this package raises. Catch this if you
    want to handle any database problem the same way, regardless of
    which specific thing went wrong."""

class FlareDatabaseNotFoundError(FlareDatabaseError):
    def __init__(self,path):
        super().__init__(
            f"No flare database found at {path}.\n"
            f"Build it from local LMSAL/NOAA data with:\n"
            f"    flare-record-build\n"
            f"or point at an existing one with the FLARE_RECORD_DB "
            f"environment variable."
        )

class FlareDatabaseInvalidError(FlareDatabaseError):
    def __init__(self, path, reason):
        super().__init__(
            f"The flare database at {path} is invalid: {reason}\n"
            f"Rebuild it with:\n"
            f"    flare-record-build"
        )