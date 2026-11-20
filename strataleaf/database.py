from .aggregate import Aggregates
from .archive import Archives
from .catalog import Catalog
from .diagnostics import Diagnostics
from .feeds import ChangeFeed
from .indexes import Indexes
from .mvcc import read_record
from .patch import patch_document
from .query import QueryEngine
from .replica import Replication
from .retention import Retention
from .sql import execute as execute_sql
from .storage import Storage
from .transaction import WriteBatch
from .transfer import Transfer


class Database:
    """An embedded, single-file, revisioned JSON document database."""
    def __init__(self, path, timeout=5.0):
        self._store = Storage(path, timeout)
        self.catalog = Catalog(self._store)
        self.indexes = Indexes(self._store)
        self.feed = ChangeFeed(self._store)
        self.retention = Retention(self._store)
        self.archives = Archives(self._store)
        self.replication = Replication(self._store)
        self.aggregates = Aggregates(self._store)
        self.transfer = Transfer(self._store)
        self.diagnostics = Diagnostics(self._store)
        self._queries = QueryEngine(self._store)

    @property
    def revision(self):
        with self._store.read():
            return int(self._store.get("head"))

    def batch(self, request_key=None):
        return WriteBatch(self._store, request_key)

    def get(self, collection, key, revision=None):
        return read_record(self._store, collection, key, revision)

    def query(self, collection, predicate=None, limit=100, cursor=None, revision=None, force_scan=False):
        return self._queries.select(collection, predicate, limit, cursor, revision, force_scan)

    def sql(self, statement, limit=100, cursor=None, revision=None, force_scan=False):
        """Run one restricted SELECT against a single collection.

        The statement is parsed by a closed grammar and lowered onto the same
        predicate, revision, cursor and grouping machinery as the Python
        query API.  No part of the text is spliced into an internal SQLite
        statement.  Errors distinguish :class:`SqlParseError` (malformed) from
        :class:`UnsupportedSql` (well-formed but outside the subset).
        """
        return execute_sql(self._queries, self.aggregates, statement, limit, cursor, revision, force_scan)

    def patch(self, collection, key, patch, expected=None):
        return patch_document(self._store, collection, key, patch, expected)

    def checkpoint(self):
        return self._store.checkpoint()

    def close(self):
        self._store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()
