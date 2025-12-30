import os
import pytest
from unittest.mock import MagicMock, patch
from mongo_orm.core import entity, BaseEntity, Config
from mongo_orm.utils import apply_all_indexes, apply_indexes, LOCK_FILE


@entity("mongoorm-test-entity")
class TestEntity(BaseEntity):
    name: str
    test: str | None = None
    __indexes__ = [
        {"keys": [("name", 1)], "unique": False},
        {"name": "test_index", "keys": [("test", -1)]},
    ]


def test():
    TestEntity(name="test").save()
    apply_all_indexes("always")
    assert (
        "test_index" in TestEntity.get_collection().index_information()
    )  # Should not raise
    TestEntity.get_collection().drop()


test()
