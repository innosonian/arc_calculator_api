"""Small conditional DynamoDB/Queue doubles for AWS assembly boundary tests."""

from copy import deepcopy
from types import SimpleNamespace

from botocore.exceptions import ClientError

from mock_journey.state import _decode, _encode


class RelayDynamo:
    def __init__(self):
        self.rows, self.calls = {}, []
        self.query_error = None
        self.after_query = None

    @staticmethod
    def key(row):
        return row["PK"], row["SK"]

    def get_item(self, **request):
        self.calls.append(("get_item", deepcopy(request)))
        assert request["ConsistentRead"] is True
        row = self.rows.get(self.key(_decode(request["Key"])))
        return {"Item": _encode(deepcopy(row))} if row is not None else {}

    def put_item(self, **request):
        self.calls.append(("put_item", deepcopy(request)))
        row = _decode(request["Item"])
        old = self.rows.get(self.key(row))
        expression = request["ConditionExpression"]
        if expression == "attribute_not_exists(PK)":
            matches = old is None
        elif old is None:
            matches = False
        else:
            matches = True
            names, values = request["ExpressionAttributeNames"], _decode(request["ExpressionAttributeValues"])
            for clause in expression.split(" AND "):
                field, operator, expected = clause.split()
                left, right = old[names[field]], values[expected]
                if operator == "=":
                    matches = matches and left == right
                elif operator == "<=":
                    matches = matches and left <= right
                elif operator == ">":
                    matches = matches and left > right
                else:
                    raise AssertionError("Unexpected condition operator")
        if not matches:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.rows[self.key(row)] = deepcopy(row)
        return {}

    def query(self, **request):
        self.calls.append(("query", deepcopy(request)))
        if self.query_error:
            raise self.query_error
        assert request["Limit"] == 1 and request["IndexName"] == "GSI1"
        values = _decode(request["ExpressionAttributeValues"])
        kind = next(value for value in values.values() if type(value) is str and value.startswith("DUE#"))
        cutoff = next(value for value in values.values() if type(value) is int)
        rows = [row for row in self.rows.values() if row.get("GSI1PK") == kind and row["GSI1SK"] <= cutoff]
        rows.sort(key=lambda row: (row["GSI1SK"], row["PK"], row["SK"]))
        cursor = _decode(request["ExclusiveStartKey"]) if "ExclusiveStartKey" in request else None
        if cursor:
            position = (cursor["GSI1SK"], cursor["PK"], cursor["SK"])
            rows = [row for row in rows if (row["GSI1SK"], row["PK"], row["SK"]) > position]
        selected = [{key: row[key] for key in ("PK", "SK", "GSI1PK", "GSI1SK")} for row in rows[:1]]
        result = {"Items": [_encode(row) for row in selected]}
        if selected and len(rows) > 1:
            result["LastEvaluatedKey"] = _encode(selected[0])
        if self.after_query:
            self.after_query()
        return result


class RelaySdk:
    def __init__(self, db=None):
        self.db = db or RelayDynamo()
        self.calls, self.logs, self.sent, self.closed = [], [], [], []
        self.business_client_created = False

    def __call__(self, service, **kwargs):
        self.calls.append((service, kwargs))
        if service == "dynamodb" and not self.business_client_created:
            self.business_client_created = True
            return self.db
        if service == "sqs":
            def send(**request):
                self.sent.append(deepcopy(request))
                return {"MessageId": "synthetic-message"}
            return SimpleNamespace(send_message=send, close=lambda: self.closed.append(service))
        assert service == "dynamodb"
        return SimpleNamespace(put_item=lambda **request: self.logs.append(deepcopy(request)),
                               close=lambda: self.closed.append("logs"))
