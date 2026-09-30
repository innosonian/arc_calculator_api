"""Scripted DynamoDB client and item serialization shared by control-flow tests.

The doubles live in tests/dynamodb_doubles.py (E-02); this module keeps the
names the control-flow tests import. test_mock_state re-exports the same objects.
"""

from tests.dynamodb_doubles import SERIALIZER, ScriptedClient, item, snapshot


__all__ = ["SERIALIZER", "ScriptedClient", "item", "snapshot"]
