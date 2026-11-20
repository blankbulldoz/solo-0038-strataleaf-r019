class DatabaseError(Exception):
    """Base error for caller-visible database failures."""

class InvalidDocument(DatabaseError):
    pass

class Conflict(DatabaseError):
    pass

class NotFound(DatabaseError):
    pass

class SnapshotTooOld(DatabaseError):
    pass

class InvalidCursor(DatabaseError):
    pass

class ReadOnly(DatabaseError):
    pass

class ReplicationError(DatabaseError):
    pass

class SqlError(DatabaseError):
    """Base error for the restricted SQL entry point."""

class SqlParseError(SqlError):
    """The statement is not well formed according to the supported grammar."""

class UnsupportedSql(SqlError):
    """The statement is valid-looking SQL but uses a feature outside the subset."""
