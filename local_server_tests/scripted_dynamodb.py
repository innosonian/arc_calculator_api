"""Finite scripted DynamoDB SDK calls for local runner control-flow tests (not collected).

The double is tests/dynamodb_doubles.ScriptedClient (E-02); this module keeps
the import path the local-server tests use. It asserts the exact sequence of
SDK operations and returns scripted answers; it does not implement DynamoDB
condition semantics. Real conditions and the due index are exercised against
DynamoDB Local by the integration suites.
"""

from tests.dynamodb_doubles import SERIALIZER, ScriptedClient, item


__all__ = ["SERIALIZER", "ScriptedClient", "item"]
