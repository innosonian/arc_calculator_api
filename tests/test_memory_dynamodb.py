"""tests/memory_dynamodb.py follows the DynamoDB behavior the journey code relies on.

PROBE_EXPECTED is written from DynamoDB's documented semantics; the same probe
runs on DynamoDB Local in integration_tests/test_v2_baseline_dynamodb.py.
"""

import pytest
from botocore.exceptions import ClientError

from tests.journey_support import create_journey_table
from tests.memory_dynamodb import PROBE_EXPECTED, MemoryDynamoDB, conformance_probe


@pytest.fixture
def table():
    client = MemoryDynamoDB()
    return client, create_journey_table(client)


def test_conformance_probe_outcomes(table):
    assert conformance_probe(*table) == PROBE_EXPECTED


def test_unsupported_expression_fails_loudly_instead_of_guessing(table):
    client, name = table
    with pytest.raises(AssertionError):
        client.put_item(TableName=name, Item={"PK": {"S": "A"}, "SK": {"S": "B"}},
                        ConditionExpression="begins_with(PK, :p)", ExpressionAttributeValues={":p": {"S": "A"}})
    with pytest.raises(AssertionError):
        client.transact_write_items(TransactItems=[{"Update": {"TableName": name}}])


def test_rejected_transaction_leaves_every_item_unchanged(table):
    client, name = table
    key = {"PK": {"S": "USER#x"}, "SK": {"S": "STATE"}}
    client.put_item(TableName=name, Item={**key, "revision": {"N": "1"}})
    with pytest.raises(ClientError) as raised:
        client.transact_write_items(TransactItems=[
            {"Put": {"TableName": name, "Item": {**key, "revision": {"N": "2"}},
                     "ConditionExpression": "#r = :r", "ExpressionAttributeNames": {"#r": "revision"},
                     "ExpressionAttributeValues": {":r": {"N": "1"}}}},
            {"Put": {"TableName": name, "Item": {"PK": {"S": "NEW#y"}, "SK": {"S": "STATE"}},
                     "ConditionExpression": "attribute_exists(PK)"}},
        ])
    assert raised.value.response["Error"]["Code"] == "TransactionCanceledException"
    assert [reason["Code"] for reason in raised.value.response["CancellationReasons"]] == [
        "None", "ConditionalCheckFailed"]
    assert client.get_item(TableName=name, Key=key, ConsistentRead=True)["Item"]["revision"] == {"N": "1"}
    assert "Item" not in client.get_item(TableName=name, Key={"PK": {"S": "NEW#y"}, "SK": {"S": "STATE"}})


def test_returned_items_are_copies(table):
    client, name = table
    key = {"PK": {"S": "A"}, "SK": {"S": "B"}}
    client.put_item(TableName=name, Item={**key, "m": {"M": {"x": {"N": "1"}}}})
    first = client.get_item(TableName=name, Key=key)["Item"]
    first["m"]["M"]["x"] = {"N": "9"}
    assert client.get_item(TableName=name, Key=key)["Item"]["m"] == {"M": {"x": {"N": "1"}}}
