"""Test-only in-memory DynamoDB client for the requests this repository issues.

Only the operations and expression grammar used by mock_journey are modelled:
get_item, put_item, transact_get_items, transact_write_items (Put and
ConditionCheck), query (table key or GSI1), scan, create_table/delete_table.
Any other request shape raises AssertionError, so a divergence fails loudly
instead of silently accepting a write DynamoDB would reject.

Semantics follow DynamoDB (and are cross-checked by the same tests on DynamoDB
Local): typed comparisons (a bool is not a number, a missing attribute makes a
comparison false), all-or-nothing transactions with CancellationReasons, one
action per item, unused expression names/values rejected, strong reads. Not
modelled: the 400 KB item limit, capacity/throttling, eventual consistency.
Never import this module from runtime code.
"""

from copy import deepcopy
from decimal import Decimal
import re
from threading import RLock

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError


_DESERIALIZER = TypeDeserializer()
_MISSING = object()
_TOKEN = re.compile(r"\s*(?:(?P<op><=|>=|<>|=|<|>)|(?P<punct>[(),.])|(?P<name>#[A-Za-z0-9_]+)"
                    r"|(?P<value>:[A-Za-z0-9_]+)|(?P<word>[A-Za-z_][A-Za-z0-9_]*))")
_FUNCTIONS = frozenset({"attribute_exists", "attribute_not_exists"})


def _error(code, operation, message="", reasons=None):
    response = {"Error": {"Code": code, "Message": message}}
    if reasons is not None:
        response["CancellationReasons"] = reasons
    return ClientError(response, operation)


def _validation(operation, message):
    return _error("ValidationException", operation, message)


def _plain(value):
    return _DESERIALIZER.deserialize(value)


def _is_number(value):
    return isinstance(value, Decimal)


def _equal(left, right):
    if type(left) is bool or type(right) is bool:
        return type(left) is bool and type(right) is bool and left == right
    if _is_number(left) or _is_number(right):
        return _is_number(left) and _is_number(right) and left == right
    if type(left) is dict and type(right) is dict:
        return left.keys() == right.keys() and all(_equal(left[key], right[key]) for key in left)
    if type(left) is list and type(right) is list:
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def _ordered(left, right, operator):
    if not ((_is_number(left) and _is_number(right)) or (type(left) is str and type(right) is str)
            or (type(left) is bytes and type(right) is bytes)):
        return False
    return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[operator]


class _Expression:
    """Recursive-descent parser for the condition/key-condition subset in use."""

    def __init__(self, text, names, values, operation):
        self.operation = operation
        self.tokens = []
        position = 0
        text = text.rstrip()
        while position < len(text):
            match = _TOKEN.match(text, position)
            if match is None or match.end() == position:
                raise AssertionError(f"Unsupported expression syntax: {text!r}")
            kind = match.lastgroup
            self.tokens.append((kind, match.group(kind)))
            position = match.end()
        self.index = 0
        self.names = names or {}
        self.values = {key: _plain(value) for key, value in (values or {}).items()}
        self.used_names, self.used_values = set(), set()
        self.tree = self._or()
        if self.index != len(self.tokens):
            raise AssertionError(f"Unsupported expression syntax: {text!r}")

    def check_usage(self, extra_names=()):
        used_names = set(self.used_names) | set(extra_names)
        used_values = set(self.used_values)
        if set(self.names) - used_names:
            raise _validation(self.operation, "Value provided in ExpressionAttributeNames unused in expressions")
        if set(self.values) - used_values:
            raise _validation(self.operation, "Value provided in ExpressionAttributeValues unused in expressions")

    def _peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else (None, None)

    def _take(self, kind=None, text=None):
        token = self._peek()
        if token[0] is None or (kind and token[0] != kind) or (text and token[1].upper() != text):
            raise AssertionError(f"Unsupported expression token {token!r}")
        self.index += 1
        return token

    def _keyword(self, word):
        kind, text = self._peek()
        return kind == "word" and text.upper() == word

    def _or(self):
        node = self._and()
        while self._keyword("OR"):
            self._take()
            node = ("or", node, self._and())
        return node

    def _and(self):
        node = self._not()
        while self._keyword("AND"):
            self._take()
            node = ("and", node, self._not())
        return node

    def _not(self):
        if self._keyword("NOT"):
            self._take()
            return ("not", self._not())
        return self._primary()

    def _primary(self):
        kind, text = self._peek()
        if kind == "punct" and text == "(":
            self._take()
            node = self._or()
            self._take("punct", ")")
            return node
        if kind == "word" and text in _FUNCTIONS:
            self._take()
            self._take("punct", "(")
            path = self._path()
            self._take("punct", ")")
            return (text, path)
        left = self._operand()
        _, operator = self._take("op")
        if operator == "<>":
            raise AssertionError("Operator <> is not used by this repository")
        return ("compare", operator, left, self._operand())

    def _operand(self):
        kind, text = self._peek()
        if kind == "value":
            self._take()
            if text not in self.values:
                raise _validation(self.operation, "An expression attribute value used in expression is not defined")
            self.used_values.add(text)
            return ("value", self.values[text])
        return ("path", self._path())

    def _path(self):
        segments = [self._segment()]
        while self._peek() == ("punct", "."):
            self._take()
            segments.append(self._segment())
        return tuple(segments)

    def _segment(self):
        kind, text = self._take()
        if kind == "name":
            if text not in self.names:
                raise _validation(self.operation, "An expression attribute name used in the document path is not defined")
            self.used_names.add(text)
            return self.names[text]
        if kind == "word" and text.upper() not in {"AND", "OR", "NOT"}:
            return text
        raise AssertionError(f"Unsupported expression path token {text!r}")

    @staticmethod
    def _resolve(item, path):
        value = item
        for segment in path:
            if type(value) is not dict or segment not in value:
                return _MISSING
            value = value[segment]
        return value

    def evaluate(self, item):
        return self._evaluate(self.tree, item or {})

    def _evaluate(self, node, item):
        kind = node[0]
        if kind == "or":
            return self._evaluate(node[1], item) or self._evaluate(node[2], item)
        if kind == "and":
            return self._evaluate(node[1], item) and self._evaluate(node[2], item)
        if kind == "not":
            return not self._evaluate(node[1], item)
        if kind == "attribute_exists":
            return self._resolve(item, node[1]) is not _MISSING
        if kind == "attribute_not_exists":
            return self._resolve(item, node[1]) is _MISSING
        _, operator, left, right = node
        values = [side[1] if side[0] == "value" else self._resolve(item, side[1]) for side in (left, right)]
        if any(value is _MISSING for value in values):
            return False
        if operator == "=":
            return _equal(*values)
        return _ordered(values[0], values[1], operator)

    def key_parts(self):
        """For a key condition: [(attribute, operator, value)] joined by AND only."""
        parts = []

        def walk(node):
            if node[0] == "and":
                walk(node[1])
                walk(node[2])
            elif node[0] == "compare" and node[2][0] == "path" and node[3][0] == "value" and len(node[2][1]) == 1:
                parts.append((node[2][1][0], node[1], node[3][1]))
            else:
                raise AssertionError("Unsupported key condition expression")

        walk(self.tree)
        return parts


class _Table:
    def __init__(self, name, key_schema, attributes, indexes):
        self.name = name
        self.hash_key = next(k["AttributeName"] for k in key_schema if k["KeyType"] == "HASH")
        self.range_key = next(k["AttributeName"] for k in key_schema if k["KeyType"] == "RANGE")
        self.types = {row["AttributeName"]: row["AttributeType"] for row in attributes}
        self.indexes = {}
        for index in indexes or ():
            schema = {k["KeyType"]: k["AttributeName"] for k in index["KeySchema"]}
            assert index["Projection"] == {"ProjectionType": "ALL"}
            self.indexes[index["IndexName"]] = (schema["HASH"], schema["RANGE"])
        self.items = {}


class MemoryDynamoDB:
    """A strongly consistent single-process table store with a DynamoDB-shaped API."""

    def __init__(self):
        self._tables = {}
        self._lock = RLock()
        self.calls = []
        # Optional test seam: called as before_call(operation, request) before
        # an operation is applied, e.g. to hold a transaction at a barrier.
        self.before_call = None

    # -- table management -------------------------------------------------
    def create_table(self, *, TableName, KeySchema, AttributeDefinitions, BillingMode=None,
                     GlobalSecondaryIndexes=None, **extra):
        assert not extra, extra
        with self._lock:
            if TableName in self._tables:
                raise _error("ResourceInUseException", "CreateTable")
            self._tables[TableName] = _Table(TableName, KeySchema, AttributeDefinitions, GlobalSecondaryIndexes)
        return {"TableDescription": {"TableName": TableName, "TableStatus": "ACTIVE"}}

    def delete_table(self, *, TableName):
        with self._lock:
            self._table(TableName, "DeleteTable")
            del self._tables[TableName]
        return {}

    def close(self):
        pass

    # -- helpers ------------------------------------------------------------
    def _table(self, name, operation):
        table = self._tables.get(name)
        if table is None:
            raise _error("ResourceNotFoundException", operation, "Cannot do operations on a non-existent table")
        return table

    def _key(self, table, item, operation, *, key_only=False):
        if key_only and set(item) != {table.hash_key, table.range_key}:
            raise _validation(operation, "The provided key element does not match the schema")
        try:
            values = []
            for name in (table.hash_key, table.range_key):
                ((kind, value),) = item[name].items()
                if kind != table.types[name]:
                    raise KeyError(name)
                values.append(value)
            return tuple(values)
        except (KeyError, ValueError, AttributeError):
            raise _validation(operation, "The provided key element does not match the schema") from None

    def _check_index_types(self, table, item, operation):
        for hash_name, range_name in table.indexes.values():
            for name in (hash_name, range_name):
                if name in item and next(iter(item[name])) != table.types[name]:
                    raise _validation(operation, "Type mismatch for Index Key")

    def _record(self, operation, request):
        self.calls.append((operation, deepcopy(request)))
        if self.before_call is not None:
            self.before_call(operation, request)

    @staticmethod
    def _condition(request, operation, *, required=False):
        text = request.get("ConditionExpression")
        if text is None:
            if required:
                raise AssertionError("ConditionCheck requires ConditionExpression")
            if request.get("ExpressionAttributeNames") or request.get("ExpressionAttributeValues"):
                raise _validation(operation, "ExpressionAttributeNames/Values can only be specified with an expression")
            return None
        expression = _Expression(text, request.get("ExpressionAttributeNames"),
                                 request.get("ExpressionAttributeValues"), operation)
        expression.check_usage()
        return expression

    @staticmethod
    def _plain_item(item):
        return {key: _plain(value) for key, value in item.items()} if item is not None else None

    # -- single-item operations --------------------------------------------
    def get_item(self, *, TableName, Key, ConsistentRead=False, **extra):
        assert not extra, extra
        with self._lock:
            self._record("GetItem", {"TableName": TableName, "Key": Key, "ConsistentRead": ConsistentRead})
            table = self._table(TableName, "GetItem")
            found = table.items.get(self._key(table, Key, "GetItem", key_only=True))
            return {"Item": deepcopy(found)} if found is not None else {}

    def put_item(self, *, TableName, Item, **request):
        assert set(request) <= {"ConditionExpression", "ExpressionAttributeNames", "ExpressionAttributeValues"}, request
        with self._lock:
            self._record("PutItem", {"TableName": TableName, "Item": Item, **request})
            table = self._table(TableName, "PutItem")
            key = self._key(table, Item, "PutItem")
            self._check_index_types(table, Item, "PutItem")
            expression = self._condition(request, "PutItem")
            if expression is not None and not expression.evaluate(self._plain_item(table.items.get(key))):
                raise _error("ConditionalCheckFailedException", "PutItem", "The conditional request failed")
            table.items[key] = deepcopy(Item)
        return {}

    # -- transactions --------------------------------------------------------
    def transact_get_items(self, *, TransactItems, **extra):
        assert not extra, extra
        with self._lock:
            self._record("TransactGetItems", {"TransactItems": TransactItems})
            if not 1 <= len(TransactItems) <= 100:
                raise _validation("TransactGetItems", "Invalid number of transaction items")
            responses, seen = [], set()
            for entry in TransactItems:
                assert set(entry) == {"Get"}, entry
                request = entry["Get"]
                assert set(request) <= {"TableName", "Key"}, request
                table = self._table(request["TableName"], "TransactGetItems")
                key = (table.name, self._key(table, request["Key"], "TransactGetItems", key_only=True))
                if key in seen:
                    raise _validation("TransactGetItems", "Transaction request cannot include multiple operations on one item")
                seen.add(key)
                found = table.items.get(key[1])
                responses.append({"Item": deepcopy(found)} if found is not None else {})
            return {"Responses": responses}

    def transact_write_items(self, *, TransactItems, **extra):
        assert not extra, extra
        with self._lock:
            self._record("TransactWriteItems", {"TransactItems": TransactItems})
            if not 1 <= len(TransactItems) <= 100:
                raise _validation("TransactWriteItems", "Invalid number of transaction items")
            planned, seen, reasons = [], set(), []
            for entry in TransactItems:
                if set(entry) == {"Put"}:
                    request = entry["Put"]
                    assert set(request) <= {"TableName", "Item", "ConditionExpression",
                                            "ExpressionAttributeNames", "ExpressionAttributeValues"}, request
                    table = self._table(request["TableName"], "TransactWriteItems")
                    key = self._key(table, request["Item"], "TransactWriteItems")
                    self._check_index_types(table, request["Item"], "TransactWriteItems")
                    expression = self._condition(request, "TransactWriteItems")
                    item = request["Item"]
                elif set(entry) == {"ConditionCheck"}:
                    request = entry["ConditionCheck"]
                    assert set(request) <= {"TableName", "Key", "ConditionExpression",
                                            "ExpressionAttributeNames", "ExpressionAttributeValues"}, request
                    table = self._table(request["TableName"], "TransactWriteItems")
                    key = self._key(table, request["Key"], "TransactWriteItems", key_only=True)
                    expression = self._condition(request, "TransactWriteItems", required=True)
                    item = None
                else:
                    raise AssertionError(f"Unsupported transaction action {sorted(entry)}")
                if (table.name, key) in seen:
                    raise _validation("TransactWriteItems",
                                      "Transaction request cannot include multiple operations on one item")
                seen.add((table.name, key))
                current = self._plain_item(table.items.get(key))
                passed = expression is None or expression.evaluate(current)
                reasons.append({"Code": "None"} if passed else
                               {"Code": "ConditionalCheckFailed", "Message": "The conditional request failed"})
                planned.append((table, key, item))
            if any(reason["Code"] != "None" for reason in reasons):
                raise _error("TransactionCanceledException", "TransactWriteItems",
                             "Transaction cancelled, please refer cancellation reasons for specific reasons",
                             reasons)
            for table, key, item in planned:
                if item is not None:
                    table.items[key] = deepcopy(item)
        return {}

    # -- reads over many items ----------------------------------------------
    def query(self, *, TableName, KeyConditionExpression, ExpressionAttributeValues, IndexName=None,
              ExpressionAttributeNames=None, Limit=None, ExclusiveStartKey=None, ProjectionExpression=None,
              ScanIndexForward=True, ConsistentRead=False, **extra):
        assert not extra, extra
        with self._lock:
            self._record("Query", {"TableName": TableName, "IndexName": IndexName,
                                   "KeyConditionExpression": KeyConditionExpression})
            table = self._table(TableName, "Query")
            if IndexName is None:
                hash_name, range_name = table.hash_key, table.range_key
            else:
                if IndexName not in table.indexes:
                    raise _validation("Query", "The table does not have the specified index")
                if ConsistentRead:
                    raise _validation("Query", "Consistent reads are not supported on global secondary indexes")
                hash_name, range_name = table.indexes[IndexName]
            condition = _Expression(KeyConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues, "Query")
            names, placeholders = self._projection(ProjectionExpression, ExpressionAttributeNames or {})
            parts = condition.key_parts()
            (hash_parts, range_parts) = ([p for p in parts if p[0] == hash_name], [p for p in parts if p[0] != hash_name])
            if len(hash_parts) != 1 or hash_parts[0][1] != "=" or len(range_parts) > 1 or (
                    range_parts and range_parts[0][0] != range_name):
                raise AssertionError("Unsupported key condition expression")
            condition.check_usage(placeholders)
            rows = []
            for item in table.items.values():
                plain = self._plain_item(item)
                if hash_name not in plain or range_name not in plain:
                    continue
                if not _equal(plain[hash_name], hash_parts[0][2]):
                    continue
                if range_parts:
                    _, operator, expected = range_parts[0]
                    matched = (_equal(plain[range_name], expected) if operator == "="
                               else _ordered(plain[range_name], expected, operator))
                    if not matched:
                        continue
                rows.append((plain[range_name], plain[table.hash_key], plain[table.range_key], item))
            rows.sort(key=lambda row: row[:3], reverse=not ScanIndexForward)
            if ExclusiveStartKey is not None:
                start = self._plain_item(ExclusiveStartKey)
                position = (start[range_name], start[table.hash_key], start[table.range_key])
                rows = [row for row in rows if (row[:3] > position if ScanIndexForward else row[:3] < position)]
            limited = rows[:Limit] if Limit is not None else rows
            items = [deepcopy({k: v for k, v in row[3].items() if names is None or k in names}) for row in limited]
            response = {"Items": items, "Count": len(items)}
            if Limit is not None and len(limited) == Limit and limited:
                # DynamoDB stops at Limit without looking ahead, so a full page
                # always carries a continuation key (possibly to an empty page).
                last = limited[-1][3]
                key_names = {table.hash_key, table.range_key, hash_name, range_name}
                response["LastEvaluatedKey"] = deepcopy({k: last[k] for k in key_names})
            return response

    @staticmethod
    def _projection(text, attribute_names):
        """Top-level attribute list only: '#a, #b' or 'PK, SK'."""
        if text is None:
            return None, ()
        names, placeholders = set(), set()
        for part in text.split(","):
            part = part.strip()
            if re.fullmatch(r"#[A-Za-z0-9_]+", part):
                if part not in attribute_names:
                    raise _validation("Query", "An expression attribute name used in the document path is not defined")
                placeholders.add(part)
                names.add(attribute_names[part])
            elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
                names.add(part)
            else:
                raise AssertionError(f"Unsupported projection {text!r}")
        return names, placeholders

    def scan(self, *, TableName, Limit=None, ExclusiveStartKey=None, ConsistentRead=False,
             ProjectionExpression=None, ExpressionAttributeNames=None, **extra):
        assert not extra, extra
        with self._lock:
            self._record("Scan", {"TableName": TableName})
            table = self._table(TableName, "Scan")
            keys = sorted(table.items)
            if ExclusiveStartKey is not None:
                start = self._key(table, ExclusiveStartKey, "Scan", key_only=True)
                keys = [key for key in keys if key > start]
            limited = keys[:Limit] if Limit is not None else keys
            names, placeholders = self._projection(ProjectionExpression, ExpressionAttributeNames or {})
            if set(ExpressionAttributeNames or {}) - set(placeholders):
                raise _validation("Scan", "Value provided in ExpressionAttributeNames unused in expressions")
            items = [deepcopy({k: v for k, v in table.items[key].items() if names is None or k in names})
                     for key in limited]
            response = {"Items": items, "Count": len(items)}
            if Limit is not None and len(limited) == Limit and limited:
                response["LastEvaluatedKey"] = deepcopy({name: table.items[limited[-1]][name]
                                                         for name in (table.hash_key, table.range_key)})
            return response


# ---------------------------------------------------------------------------
# Conformance probe: the same requests against MemoryDynamoDB and DynamoDB Local
# must give PROBE_EXPECTED (tests/test_memory_dynamodb.py and
# integration_tests/test_v2_baseline_dynamodb.py). The table needs GSI1.
# ---------------------------------------------------------------------------

PROBE_EXPECTED = [
    ("put_new", "ok"), ("put_again_if_absent", "ConditionalCheckFailedException"),
    ("bool_is_not_number", "ConditionalCheckFailedException"), ("nested_path_match", "ok"),
    ("missing_attribute_compare", "ConditionalCheckFailedException"),
    ("unused_value", "ValidationException"),
    ("transaction_cancelled", ["None", "ConditionalCheckFailed"]), ("no_partial_write", True),
    ("duplicate_item_in_transaction", "ValidationException"),
    ("or_group", "ok"),
    ("gsi_page_1", [["DUE#JOB", 4, "JOB#a"]]), ("gsi_page_1_has_key", True),
    ("gsi_page_2", [["DUE#JOB", 5, "JOB#b"]]), ("gsi_page_3", [["DUE#JOB", 7, "JOB#c"]]),
    ("gsi_projection", ["GSI1PK", "GSI1SK", "PK", "SK"]),
    ("gsi_consistent_read", "ValidationException"),
]


def conformance_probe(client, table):
    """Run documented DynamoDB behaviors the journey code relies on; return the outcomes."""
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()
    outcomes = []

    def attempt(name, call):
        try:
            call()
            outcomes.append((name, "ok"))
        except ClientError as error:
            outcomes.append((name, error.response["Error"]["Code"]))

    def item(pk, **values):
        return {"PK": {"S": pk}, "SK": {"S": "STATE"}, **{k: serializer.serialize(v) for k, v in values.items()}}

    new = item("USER#probe", flag=True, slots={"a:b": {"completed": False}}, revision=1)
    attempt("put_new", lambda: client.put_item(TableName=table, Item=new,
                                               ConditionExpression="attribute_not_exists(PK)"))
    attempt("put_again_if_absent", lambda: client.put_item(TableName=table, Item=new,
                                                           ConditionExpression="attribute_not_exists(PK)"))
    attempt("bool_is_not_number", lambda: client.put_item(
        TableName=table, Item=new, ConditionExpression="#f = :v",
        ExpressionAttributeNames={"#f": "flag"}, ExpressionAttributeValues={":v": {"N": "1"}}))
    attempt("nested_path_match", lambda: client.put_item(
        TableName=table, Item=new, ConditionExpression="#r = :r AND #s.#k.#c = :false",
        ExpressionAttributeNames={"#r": "revision", "#s": "slots", "#k": "a:b", "#c": "completed"},
        ExpressionAttributeValues={":r": {"N": "1"}, ":false": {"BOOL": False}}))
    attempt("missing_attribute_compare", lambda: client.put_item(
        TableName=table, Item=new, ConditionExpression="#m > :v",
        ExpressionAttributeNames={"#m": "missing"}, ExpressionAttributeValues={":v": {"N": "0"}}))
    attempt("unused_value", lambda: client.put_item(
        TableName=table, Item=new, ConditionExpression="#r = :r",
        ExpressionAttributeNames={"#r": "revision"},
        ExpressionAttributeValues={":r": {"N": "1"}, ":unused": {"N": "2"}}))
    try:
        client.transact_write_items(TransactItems=[
            {"Put": {"TableName": table, "Item": item("OTHER#probe", n=1),
                     "ConditionExpression": "attribute_not_exists(PK)"}},
            {"ConditionCheck": {"TableName": table, "Key": {"PK": {"S": "USER#probe"}, "SK": {"S": "STATE"}},
                                "ConditionExpression": "#r = :r", "ExpressionAttributeNames": {"#r": "revision"},
                                "ExpressionAttributeValues": {":r": {"N": "9"}}}},
        ])
        outcomes.append(("transaction_cancelled", "committed"))
    except ClientError as error:
        outcomes.append(("transaction_cancelled",
                         [reason.get("Code") for reason in error.response.get("CancellationReasons", [])]))
    found = client.get_item(TableName=table, Key={"PK": {"S": "OTHER#probe"}, "SK": {"S": "STATE"}},
                            ConsistentRead=True)
    outcomes.append(("no_partial_write", "Item" not in found))
    attempt("duplicate_item_in_transaction", lambda: client.transact_write_items(TransactItems=[
        {"Put": {"TableName": table, "Item": item("DUP#probe", n=1)}},
        {"Put": {"TableName": table, "Item": item("DUP#probe", n=2)}},
    ]))
    attempt("or_group", lambda: client.put_item(
        TableName=table, Item=new,
        ConditionExpression="#r = :r AND (#f = :no OR (#f = :yes AND #r <= :r))",
        ExpressionAttributeNames={"#r": "revision", "#f": "flag"},
        ExpressionAttributeValues={":r": {"N": "1"}, ":no": {"BOOL": False}, ":yes": {"BOOL": True}}))
    # Distinct due values: DynamoDB leaves the order of equal index keys unspecified.
    for pk, due in (("JOB#b", 5), ("JOB#c", 7), ("JOB#a", 4), ("JOB#late", 99)):
        client.put_item(TableName=table, Item=item(pk, GSI1PK="DUE#JOB", GSI1SK=due, other="x"))
    request = {"TableName": table, "IndexName": "GSI1", "Limit": 1,
               "KeyConditionExpression": "#pk = :kind AND #sk <= :now",
               "ExpressionAttributeNames": {"#pk": "GSI1PK", "#sk": "GSI1SK"},
               "ExpressionAttributeValues": {":kind": {"S": "DUE#JOB"}, ":now": {"N": "10"}}}
    for page in (1, 2, 3):
        response = client.query(**request)
        outcomes.append((f"gsi_page_{page}", [[row["GSI1PK"]["S"], int(row["GSI1SK"]["N"]), row["PK"]["S"]]
                                              for row in response["Items"]]))
        if page == 1:
            outcomes.append(("gsi_page_1_has_key", "LastEvaluatedKey" in response))
        request["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    projected = client.query(
        TableName=table, IndexName="GSI1", Limit=1, ProjectionExpression="#p, #s, #gpk, #gsk",
        KeyConditionExpression="#gpk = :kind AND #gsk <= :now",
        ExpressionAttributeNames={"#p": "PK", "#s": "SK", "#gpk": "GSI1PK", "#gsk": "GSI1SK"},
        ExpressionAttributeValues={":kind": {"S": "DUE#JOB"}, ":now": {"N": "10"}})
    outcomes.append(("gsi_projection", sorted(projected["Items"][0])))
    attempt("gsi_consistent_read", lambda: client.query(
        TableName=table, IndexName="GSI1", ConsistentRead=True,
        KeyConditionExpression="#pk = :kind", ExpressionAttributeNames={"#pk": "GSI1PK"},
        ExpressionAttributeValues={":kind": {"S": "DUE#JOB"}}))
    return outcomes
