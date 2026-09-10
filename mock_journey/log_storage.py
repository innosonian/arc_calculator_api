"""Append-only operational rows, separate from session/job keys and due GSI.

A supplied client owns explicit environment/endpoint/permissions. No default
AWS discovery, migrations, TTL, deletion, or HTTP admin route is provided.
"""

from datetime import datetime
import hashlib
import json
import re

from services.operational_logs import MAX_RECORD_BYTES, validate_record


class OperationalLogError(RuntimeError):
    def __init__(self):
        super().__init__("Operational logs are unavailable or invalid.")


def _parse(raw):
    if type(raw) is not bytes or len(raw) > MAX_RECORD_BYTES:
        raise OperationalLogError()
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise OperationalLogError()
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs)
    if validate_record(value) != raw:
        raise OperationalLogError()
    return value


class DynamoLogStore:
    def __init__(self, client, table_name, environment, *, owns_client=True):
        if (client is None or type(table_name) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", table_name)
                or type(environment) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", environment)):
            raise OperationalLogError()
        self.client, self.table = client, table_name
        self.namespace = "OPS#" + hashlib.sha256(environment.encode()).hexdigest()
        self.owns_client = owns_client

    def write(self, raw):
        try:
            value = _parse(raw)
            key = {"PK": {"S": self.namespace + "#" + value["occurred_at"][:10]},
                   "SK": {"S": value["occurred_at"] + "#" + value["log_id"]}}
            item = {**key, "kind": {"S": "operational_log_v1"}, "record": {"S": raw.decode()},
                    "sha256": {"S": hashlib.sha256(raw).hexdigest()}}
            try:
                self.client.put_item(TableName=self.table, Item=item, ConditionExpression="attribute_not_exists(PK)")
            except Exception as error:
                if getattr(error, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
                found = self.client.get_item(TableName=self.table, Key=key, ConsistentRead=True).get("Item")
                if found != item:
                    raise OperationalLogError()
        except Exception:
            raise OperationalLogError() from None

    def read_page(self, day, *, limit=100, after=None):
        try:
            if (type(day) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
                    or type(limit) is not int or not 1 <= limit <= 500):
                raise OperationalLogError()
            datetime.strptime(day, "%Y-%m-%d")
            partition = self.namespace + "#" + day
            args = {"TableName": self.table, "KeyConditionExpression": "PK = :pk",
                    "ExpressionAttributeValues": {":pk": {"S": partition}}, "ConsistentRead": True,
                    "Limit": limit, "ScanIndexForward": True}
            if after is not None:
                if (type(after) is not str or not re.fullmatch(
                        re.escape(day) + r"T\d{2}:\d{2}:\d{2}\.\d{6}Z#[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", after)):
                    raise OperationalLogError()
                args["ExclusiveStartKey"] = {"PK": {"S": partition}, "SK": {"S": after}}
            result = self.client.query(**args)
            items = result.get("Items", [])
            if type(items) is not list or len(items) > limit:
                raise OperationalLogError()
            records = []
            for item in items:
                if set(item) != {"PK", "SK", "kind", "record", "sha256"}:
                    raise OperationalLogError()
                raw = item["record"]["S"].encode()
                value = _parse(raw)
                if (item["PK"] != {"S": partition} or value["occurred_at"][:10] != day
                        or item["SK"] != {"S": value["occurred_at"] + "#" + value["log_id"]}
                        or item["kind"] != {"S": "operational_log_v1"}
                        or item["sha256"] != {"S": hashlib.sha256(raw).hexdigest()}):
                    raise OperationalLogError()
                records.append(value)
            cursor = result.get("LastEvaluatedKey")
            if cursor and (not items or cursor != {k: items[-1][k] for k in ("PK", "SK")}):
                raise OperationalLogError()
            return {"records": records, "next_cursor": cursor["SK"]["S"] if cursor else None}
        except Exception:
            raise OperationalLogError() from None

    def close(self):
        if self.owns_client:
            self.client.close()
