"""Restricted SQL entry point.

Statements are tokenized, parsed and lowered onto the same predicate objects,
read revisions, cursors and grouping code used by the Python query API.  User
text is never interpolated into a SQLite statement: only literals parsed out of
the statement ever reach the query engine, and those travel as JSON values.
"""
from dataclasses import dataclass

from .codec import MISSING, field as checked_field, name as checked_name
from .errors import InvalidDocument, SqlParseError, UnsupportedSql


AGGREGATES = {"count", "sum", "min", "max", "avg"}
COMPARISONS = {
    "=": "eq", "==": "eq", "!=": "ne", "<>": "ne",
    "<": "lt", "<=": "le", ">": "gt", ">=": "ge",
}
UNSUPPORTED_CLAUSES = {
    "order", "having", "offset", "union", "intersect", "except", "join", "inner",
    "left", "right", "full", "cross", "natural", "using", "window", "fetch", "for",
    "returning", "distinct", "top", "into",
}
NON_SELECT = {
    "insert", "update", "delete", "replace", "drop", "create", "alter", "pragma",
    "attach", "detach", "vacuum", "explain", "with", "begin", "commit", "rollback",
    "savepoint", "release", "analyze", "reindex",
}
STRUCTURAL = {"from", "where", "group", "limit", "select", "order", "having"}
MAX_NESTING = 16
MAX_BOOLEAN_TERMS = 64
MAX_STATEMENT_LENGTH = 100_000


@dataclass(frozen=True)
class SqlResult:
    """Result of the restricted SQL entry point.

    ``rows`` are dictionaries.  Document selections contain ``key``,
    ``revision`` and a ``document`` that is either the whole document or the
    selected subset.  Grouped selections contain one row per group with the
    selected group fields and aggregate columns.  ``cursor`` follows exactly
    the same signed continuation rules as :meth:`Database.query`.
    """
    rows: list
    revision: int
    cursor: str | None
    plan: dict
    columns: list


@dataclass(frozen=True)
class _Token:
    kind: str
    value: object
    position: int


def _syntax(message, token):
    return SqlParseError(f"{message} at position {token.position}")


def _tokenize(statement):
    if not isinstance(statement, str):
        raise SqlParseError("SQL statement must be a string")
    if len(statement) > MAX_STATEMENT_LENGTH:
        raise UnsupportedSql(f"statement exceeds {MAX_STATEMENT_LENGTH} characters")
    tokens, index = [], 0
    while index < len(statement):
        char = statement[index]
        if char.isspace():
            index += 1
            continue
        if char == "'":
            start, index, value = index, index + 1, []
            while index < len(statement):
                if statement[index] == "'":
                    if index + 1 < len(statement) and statement[index + 1] == "'":
                        value.append("'")
                        index += 2
                        continue
                    index += 1
                    break
                value.append(statement[index])
                index += 1
            else:
                raise SqlParseError(f"unterminated string literal at position {start}")
            tokens.append(_Token("string", "".join(value), start))
            continue
        if char.isdigit():
            start, index = index, index + 1
            while index < len(statement) and statement[index].isdigit():
                index += 1
            if index < len(statement) and statement[index] == ".":
                index += 1
                if index >= len(statement) or not statement[index].isdigit():
                    raise SqlParseError(f"malformed number at position {start}")
                while index < len(statement) and statement[index].isdigit():
                    index += 1
            if index < len(statement) and statement[index] in "eE":
                index += 1
                if index < len(statement) and statement[index] in "+-":
                    index += 1
                if index >= len(statement) or not statement[index].isdigit():
                    raise SqlParseError(f"malformed number at position {start}")
                while index < len(statement) and statement[index].isdigit():
                    index += 1
            if index < len(statement) and (statement[index].isalnum() or statement[index] == "_"):
                raise SqlParseError(f"malformed number at position {start}")
            text = statement[start:index]
            try:
                number = float(text) if ("." in text or "e" in text.lower()) else int(text)
            except ValueError as exc:
                raise SqlParseError(f"malformed number at position {start}") from exc
            if isinstance(number, float) and (number != number or number in (float("inf"), float("-inf"))):
                raise SqlParseError(f"numeric literal must be finite at position {start}")
            tokens.append(_Token("number", number, start))
            continue
        if char.isalpha() or char == "_":
            start, index = index, index + 1
            while index < len(statement) and (statement[index].isalnum() or statement[index] == "_"):
                index += 1
            tokens.append(_Token("ident", statement[start:index], start))
            continue
        if statement.startswith("--", index) or statement.startswith("/*", index):
            raise UnsupportedSql(f"SQL comments are not supported at position {index}")
        if char in "?$":
            raise UnsupportedSql(f"bind parameters are not supported at position {index}")
        if char == ":":
            raise UnsupportedSql(f"named parameters are not supported at position {index}")
        pair = statement[index:index + 2]
        if pair in {"!=", "<>", "<=", ">=", "=="}:
            tokens.append(_Token("punct", pair, index))
            index += 2
            continue
        if char in "(),.*=<>;+-":
            tokens.append(_Token("punct", char, index))
            index += 1
            continue
        raise SqlParseError(f"unexpected character {char!r} at position {index}")
    tokens.append(_Token("eof", None, len(statement)))
    return tokens


class _Parser:
    def __init__(self, statement):
        self.tokens = _tokenize(statement)
        self.index = 0
        self.depth = 0

    def _peek(self, offset=0):
        return self.tokens[min(self.index + offset, len(self.tokens) - 1)]

    def _advance(self):
        token = self._peek()
        if self.index < len(self.tokens) - 1:
            self.index += 1
        return token

    def _word(self, value, offset=0):
        token = self._peek(offset)
        return token.kind == "ident" and token.value.lower() == value

    def _word_in(self, values, offset=0):
        token = self._peek(offset)
        return token.kind == "ident" and token.value.lower() in values

    def _match(self, value):
        if self._word(value):
            self._advance()
            return True
        return False

    def _match_punct(self, value):
        token = self._peek()
        if token.kind == "punct" and token.value == value:
            self._advance()
            return True
        return False

    def _expect_word(self, value):
        if not self._match(value):
            raise _syntax(f"expected {value.upper()}", self._peek())

    def _expect_punct(self, value):
        if not self._match_punct(value):
            raise _syntax(f"expected {value!r}", self._peek())

    def _expect_ident(self, purpose):
        token = self._peek()
        if token.kind != "ident":
            raise _syntax(f"expected {purpose}", token)
        self._advance()
        return token

    def _path(self):
        parts = [self._expect_ident("a field name").value]
        while self._match_punct("."):
            parts.append(self._expect_ident("a field name").value)
        return ".".join(parts)

    def _alias(self):
        if self._match("as"):
            return self._expect_ident("an alias").value
        return None

    def statement(self):
        first = self._peek()
        if first.kind == "eof":
            raise SqlParseError("empty SQL statement")
        if first.kind != "ident":
            raise _syntax("statement must begin with SELECT", first)
        keyword = first.value.lower()
        if keyword != "select":
            if keyword in NON_SELECT:
                raise UnsupportedSql(f"only SELECT statements are supported, got {first.value.upper()} at position {first.position}")
            raise _syntax(f"unsupported statement keyword {first.value!r}", first)
        self._advance()
        if self._word("distinct"):
            raise UnsupportedSql(f"SELECT DISTINCT is not supported at position {self._peek().position}")
        if self._word("top"):
            raise UnsupportedSql(f"SELECT TOP is not supported at position {self._peek().position}")
        star = self._match_punct("*")
        items = []
        if not star:
            items.append(self._select_item())
            while self._match_punct(","):
                items.append(self._select_item())
        elif not self._word("from"):
            raise _syntax("'*' must be the only selected item", self._peek())
        self._expect_word("from")
        collection = checked_name(self._expect_ident("a collection name").value)
        token = self._peek()
        if token.kind == "punct" and token.value == ",":
            raise UnsupportedSql(f"multiple collections are not supported at position {token.position}")
        if self._word_in({"join", "inner", "left", "right", "full", "cross", "natural"}):
            raise UnsupportedSql(f"joins are not supported at position {token.position}")
        predicate = None
        if self._match("where"):
            predicate = self._expression()
        group_by = []
        if self._match("group"):
            self._expect_word("by")
            group_by.append(self._path())
            while self._match_punct(","):
                group_by.append(self._path())
        limit = None
        if self._match("limit"):
            limit = self._limit()
        if self._match_punct(";"):
            if self._peek().kind != "eof":
                raise UnsupportedSql(f"multiple statements are not supported at position {self._peek().position}")
        token = self._peek()
        if token.kind != "eof":
            if token.kind == "ident":
                word = token.value.lower()
                if word in UNSUPPORTED_CLAUSES:
                    raise UnsupportedSql(f"{word.upper()} is not supported at position {token.position}")
                if word in STRUCTURAL:
                    raise _syntax(f"misplaced {word.upper()} clause", token)
                raise UnsupportedSql(f"unexpected clause {token.value!r}; only a single collection is supported at position {token.position}")
            raise _syntax(f"unexpected input {token.value!r}", token)
        return self._compile(collection, star, items, predicate, group_by, limit)

    def _select_item(self):
        token = self._peek()
        if token.kind != "ident" or token.value.lower() in STRUCTURAL:
            raise _syntax("expected a selected field or aggregate", token)
        if token.value.lower() in AGGREGATES and self._peek(1).kind == "punct" and self._peek(1).value == "(":
            function = token.value.lower()
            self.index += 2
            if self._word("distinct"):
                raise UnsupportedSql(f"DISTINCT inside aggregates is not supported at position {self._peek().position}")
            path = None
            if self._match_punct("*"):
                if function != "count":
                    raise SqlParseError(f"{function.upper()} requires a field name at position {token.position}")
            else:
                if self._word("select"):
                    raise UnsupportedSql(f"subqueries are not supported at position {self._peek().position}")
                path = self._path()
            self._expect_punct(")")
            return {"kind": "aggregate", "func": function, "path": path, "alias": self._alias()}
        path = self._path()
        if self._peek().kind == "punct" and self._peek().value == "(":
            raise UnsupportedSql(f"function {path} is not supported at position {token.position}")
        return {"kind": "field", "path": path, "alias": self._alias()}

    def _limit(self):
        token = self._peek()
        sign = 1
        if self._match_punct("-"):
            sign = -1
        else:
            self._match_punct("+")
        token = self._peek()
        if token.kind != "number" or isinstance(token.value, float):
            raise _syntax("LIMIT requires an integer literal", token)
        self._advance()
        value = sign * token.value
        if not 1 <= value <= 1000:
            raise InvalidDocument("page size must be between 1 and 1000")
        if self._peek().kind == "punct" and self._peek().value == ",":
            raise UnsupportedSql(f"LIMIT with an offset is not supported at position {self._peek().position}")
        return value

    def _expression(self):
        node = self._and_expression()
        if self._word("or"):
            children = [node]
            while self._match("or"):
                children.append(self._and_expression())
                if len(children) > MAX_BOOLEAN_TERMS:
                    raise UnsupportedSql(f"OR expression exceeds {MAX_BOOLEAN_TERMS} terms")
            return {"or": children}
        return node

    def _and_expression(self):
        node = self._not_expression()
        if self._word("and"):
            children = [node]
            while self._match("and"):
                children.append(self._not_expression())
                if len(children) > MAX_BOOLEAN_TERMS:
                    raise UnsupportedSql(f"AND expression exceeds {MAX_BOOLEAN_TERMS} terms")
            return {"and": children}
        return node

    def _not_expression(self):
        if self._word("not"):
            self._advance()
            self._enter()
            try:
                return {"not": self._not_expression()}
            finally:
                self.depth -= 1
        return self._primary()

    def _enter(self):
        self.depth += 1
        if self.depth > MAX_NESTING:
            raise UnsupportedSql(f"expression nesting exceeds {MAX_NESTING} levels")

    def _primary(self):
        token = self._peek()
        if token.kind == "punct" and token.value == "(":
            self._advance()
            self._enter()
            try:
                node = self._expression()
            finally:
                self.depth -= 1
            self._expect_punct(")")
            return node
        if self._word_in({"exists", "select"}) and self._peek(1).kind == "punct" and self._peek(1).value == "(":
            raise UnsupportedSql(f"subqueries are not supported at position {token.position}")
        path = self._path()
        if self._peek().kind == "punct" and self._peek().value == "(":
            raise UnsupportedSql(f"function calls in WHERE are not supported at position {token.position}")
        if self._match("is"):
            return self._is_expression(path, token)
        if self._match("not"):
            if self._match("in"):
                return {"not": {"field": path, "op": "in", "value": self._in_list()}}
            if self._match("contains"):
                return {"not": {"field": path, "op": "contains", "value": self._non_null_literal()}}
            if self._word("like"):
                raise UnsupportedSql(f"LIKE is not supported at position {self._peek().position}")
            if self._word("between"):
                raise UnsupportedSql(f"BETWEEN is not supported at position {self._peek().position}")
            raise _syntax("expected IN or CONTAINS after NOT", self._peek())
        if self._match("in"):
            return {"field": path, "op": "in", "value": self._in_list()}
        if self._match("contains"):
            return {"field": path, "op": "contains", "value": self._non_null_literal()}
        if self._word("like"):
            raise UnsupportedSql(f"LIKE is not supported at position {self._peek().position}")
        if self._word("between"):
            raise UnsupportedSql(f"BETWEEN is not supported at position {self._peek().position}")
        token = self._peek()
        if token.kind != "punct" or token.value not in COMPARISONS:
            raise _syntax("expected a comparison operator", token)
        self._advance()
        return {"field": path, "op": COMPARISONS[token.value], "value": self._non_null_literal()}

    def _non_null_literal(self):
        token = self._peek()
        value = self._literal()
        if value is None:
            raise UnsupportedSql(f"comparison with NULL is not supported; use IS NULL at position {token.position}")
        return value

    def _is_expression(self, path, token):
        negated = self._match("not")
        word = self._peek()
        if word.kind != "ident" or word.value.lower() not in {"null", "missing"}:
            raise UnsupportedSql(f"IS only supports NULL and MISSING at position {word.position}")
        self._advance()
        kind = word.value.lower()
        if kind == "null":
            expression = {"field": path, "op": "eq", "value": None}
            if negated:
                return {"and": [{"field": path, "op": "exists", "value": True}, {"not": expression}]}
            return expression
        return {"field": path, "op": "exists", "value": negated}

    def _in_list(self):
        self._expect_punct("(")
        if self._word("select"):
            raise UnsupportedSql(f"subqueries are not supported at position {self._peek().position}")
        values = [self._literal()]
        if values[0] is None:
            raise UnsupportedSql(f"NULL inside IN is not supported; use IS NULL at position {self._peek().position}")
        while self._match_punct(","):
            value = self._literal()
            if value is None:
                raise UnsupportedSql(f"NULL inside IN is not supported; use IS NULL at position {self._peek().position}")
            values.append(value)
        self._expect_punct(")")
        return values

    def _literal(self):
        token = self._peek()
        if token.kind in {"string", "number"}:
            self._advance()
            return token.value
        if token.kind == "punct" and token.value in "+-":
            self._advance()
            number = self._peek()
            if number.kind != "number":
                raise _syntax("expected a numeric literal", number)
            self._advance()
            return number.value if token.value == "+" else -number.value
        if token.kind == "ident":
            word = token.value.lower()
            if word in {"true", "false"}:
                self._advance()
                return word == "true"
            if word == "null":
                self._advance()
                return None
        raise _syntax("expected a literal value", token)

    def _compile(self, collection, star, items, predicate, group_by, limit):
        for path in group_by:
            checked_field({}, path)
        seen = set()
        for path in group_by:
            if path in seen:
                raise SqlParseError("duplicate field in GROUP BY: " + path)
            seen.add(path)
        aggregates = [item for item in items if item["kind"] == "aggregate"]
        for item in items:
            if item["path"] is not None:
                checked_field({}, item["path"])
        if star:
            if group_by:
                raise UnsupportedSql("SELECT * cannot be combined with GROUP BY")
            return _Plan(collection, predicate, True, items, group_by, limit, False,
                         ["*"])
        if aggregates:
            if limit is not None:
                raise UnsupportedSql("LIMIT is not supported for grouped queries")
            for item in items:
                if item["kind"] == "field" and item["path"] not in group_by:
                    raise UnsupportedSql("field " + item["path"] + " must appear in GROUP BY or be aggregated")
            mode_grouped = True
        elif group_by:
            for item in items:
                if item["path"] not in group_by:
                    raise UnsupportedSql("field " + item["path"] + " must appear in GROUP BY")
            mode_grouped = True
        else:
            mode_grouped = False
        columns = []
        for item in items:
            if item["alias"] is not None:
                columns.append(item["alias"])
            elif item["kind"] == "field":
                columns.append(item["path"])
            else:
                columns.append(item["func"])
        if len(set(columns)) != len(columns):
            raise SqlParseError("duplicate output column; use AS to distinguish selected fields")
        return _Plan(collection, predicate, False, items, group_by, limit, mode_grouped, columns)


@dataclass(frozen=True)
class _Plan:
    collection: str
    predicate: dict | None
    star: bool
    items: list
    group_by: list
    limit: int | None
    grouped: bool
    columns: list


def compile_statement(statement):
    """Parse a restricted SELECT statement into an execution plan.

    Exposed for inspection and documentation; executing a plan still goes
    through the ordinary query and aggregate engines.
    """
    return _Parser(statement).statement()


def _document_row(plan, record):
    if plan.star:
        document = record.document
    else:
        document = {}
        for item in plan.items:
            value = checked_field(record.document, item["path"])
            if value is not MISSING:
                document[item["alias"] or item["path"]] = value
    return {"key": record.key, "revision": record.revision, "document": document}


def _aggregate_value(item, bucket):
    function, path = item["func"], item["path"]
    if function == "count" and path is None:
        return bucket["count"]
    stats = bucket["measures"].get(path)
    if stats is None:
        return None
    if function == "count":
        return stats["present"]
    if function == "sum":
        return stats["sum"]
    if function == "min":
        return stats["min"]
    if function == "max":
        return stats["max"]
    return stats["sum"] / stats["numeric_count"] if stats["numeric_count"] else None


def _aggregate_rows(plan, buckets):
    positions = {path: index for index, path in enumerate(plan.group_by)}
    rows = []
    for bucket in buckets:
        row = {}
        for item in plan.items:
            name = item["alias"] or (item["path"] if item["kind"] == "field" else item["func"])
            if item["kind"] == "field":
                row[name] = bucket["values"][positions[item["path"]]]
            else:
                row[name] = _aggregate_value(item, bucket)
        rows.append(row)
    return rows


def execute(query_engine, aggregates, statement, limit=100, cursor=None, revision=None, force_scan=False):
    plan = compile_statement(statement)
    if plan.grouped:
        if cursor is not None:
            raise UnsupportedSql("continuation cursors are only supported for document selections")
        measure_paths = [item["path"] for item in plan.items
                         if item["kind"] == "aggregate" and item["path"] is not None]
        scanned = aggregates.buckets(plan.collection, plan.group_by, measure_paths, plan.predicate, revision)
        rows = _aggregate_rows(plan, scanned["buckets"])
        return SqlResult(rows, scanned["revision"], None,
                         {"access": "version_scan", "index": None, "examined": scanned["examined"]},
                         plan.columns)
    page = query_engine.select(plan.collection, plan.predicate,
                               plan.limit if plan.limit is not None else limit,
                               cursor, revision, force_scan)
    rows = [_document_row(plan, record) for record in page.records]
    return SqlResult(rows, page.revision, page.cursor, page.plan, plan.columns)
