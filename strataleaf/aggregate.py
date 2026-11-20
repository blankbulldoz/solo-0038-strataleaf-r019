from .codec import MISSING, dumps, field, loads
from .errors import NotFound
from .mvcc import checked_revision, visible_rows
from .predicates import matches, validate_predicate


def blank_bucket(values, measures):
    return {"values": list(values), "count": 0,
            "measures": {path: {"present": 0, "numeric_count": 0, "sum": 0, "min": None, "max": None}
                         for path in measures}}


def sort_token(values):
    """Order groups the way the Python API always has.

    A single grouping field keeps its historical token (the bare value), and
    multi-field grouping uses the JSON array of all values.
    """
    return dumps(values[0]) if len(values) == 1 else dumps(list(values))


def add_to_bucket(bucket, document, measures):
    bucket["count"] += 1
    for path in measures:
        stats = bucket["measures"][path]
        value = field(document, path)
        if value is MISSING:
            continue
        stats["present"] += 1
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            stats["numeric_count"] += 1
            stats["sum"] += value
            stats["min"] = value if stats["min"] is None else min(stats["min"], value)
            stats["max"] = value if stats["max"] is None else max(stats["max"], value)


class Aggregates:
    def __init__(self, store):
        self.store = store

    def buckets(self, collection, by, measures, predicate=None, revision=None):
        """Group a fixed read view by one or more field paths.

        Rows whose grouping path is missing or is not a usable JSON value are
        skipped; an explicit null is a value and forms its own group.  Measures
        are reported separately as presence, numeric count, sum, min and max
        so callers can apply their own numeric policy.
        """
        for path in list(by) + list(measures):
            field({}, path)
        validate_predicate(predicate)
        with self.store.read() as connection:
            revision = checked_revision(self.store, revision)
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection")
            groups, examined = {}, 0
            for row in visible_rows(connection, collection, revision):
                document = loads(row["body"])
                examined += 1
                if not matches(document, predicate):
                    continue
                values = [field(document, path) for path in by]
                if any(value is MISSING for value in values):
                    continue
                token = sort_token(values)
                bucket = groups.get(token)
                if bucket is None:
                    bucket = groups[token] = blank_bucket(values, measures)
                add_to_bucket(bucket, document, measures)
            ordered = [groups[token] for token in sorted(groups)]
            if not by and not ordered:
                ordered = [blank_bucket([], measures)]
            return {"revision": revision, "examined": examined, "buckets": ordered}

    def group(self, collection, by, measure=None, predicate=None, revision=None):
        field({}, by)
        if measure is not None:
            field({}, measure)
        measures = [] if measure is None else [measure]
        scanned = self.buckets(collection, [by], measures, predicate, revision)
        output = []
        for bucket in scanned["buckets"]:
            stats = bucket["measures"].get(measure) if measure is not None else None
            numeric = stats["numeric_count"] if stats is not None else 0
            item = {"key": bucket["values"][0], "count": bucket["count"],
                    "numeric_count": numeric,
                    "sum": stats["sum"] if stats is not None else 0,
                    "min": stats["min"] if stats is not None else None,
                    "max": stats["max"] if stats is not None else None}
            item["average"] = item["sum"] / numeric if numeric else None
            output.append(item)
        return {"revision": scanned["revision"], "groups": output}
