"""Datetime serialization: entities must write BSON dates, not ISO strings.

``_prepare_save`` used ``model_dump(mode="json")``, which turns every datetime
into an ISO string on the way into Mongo. Nothing broke in application code —
pydantic parses the string back on read — but everything Mongo does server-side
did: BSON orders String before Date so mixed fields sort wrongly, ``$gt``
against a date never matches a string, and partial indexes filtered on
``{"$type": "date"}`` index nothing.

These tests run fully offline. ``_prepare_save`` opens no connection, and
``bson.encode``/``bson.decode`` is exactly the conversion the driver performs on
the wire, so encoding the payload and decoding it with the client's codec
options reproduces a real write/read round trip without a server.
"""

import datetime
import decimal
import uuid
from enum import Enum, IntEnum, StrEnum

import bson
import pytest
from bson.codec_options import CodecOptions
from pydantic import BaseModel

from mongo_orm.core import (
    _build_async_client,
    _build_sync_client,
    BaseEntity,
    entity,
)

UTC = datetime.timezone.utc
# Microseconds stay at 0 in the fixtures: BSON stores milliseconds, so a value
# carrying microseconds cannot survive a round trip (locked in by its own test).
SUBMITTED = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
NESTED = datetime.datetime(2025, 5, 6, 7, 8, 9, tzinfo=UTC)
IN_LIST = datetime.datetime(2024, 9, 10, 11, 12, 13, tzinfo=UTC)

URI = "mongodb://localhost:27017"


class Colour(StrEnum):
    RED = "red"


class Level(IntEnum):
    HIGH = 3


class Stamp(BaseModel):
    at: datetime.datetime | None = None
    label: str = ""


@entity(collection_name="dt-serialization-test")
class Doc(BaseEntity):
    id: str = "doc-1"
    submitted_at: datetime.datetime | None = None
    missing_at: datetime.datetime | None = None
    nested: Stamp | None = None
    stamps: list[Stamp] = []
    times: list[datetime.datetime] = []
    colour: Colour = Colour.RED
    level: Level = Level.HIGH
    title: str = "t"
    qty: int = 2
    flag: bool = True
    tags: list[str] = []


def build() -> Doc:
    return Doc(
        submitted_at=SUBMITTED,
        nested=Stamp(at=NESTED, label="n"),
        stamps=[Stamp(at=IN_LIST, label="l")],
        times=[IN_LIST],
        tags=["a", "b"],
    )


def payload_of(doc: BaseEntity) -> dict:
    """The ``$set`` document ``_prepare_save`` would hand to the driver."""
    update_kwargs, _callback = doc._prepare_save()
    return update_kwargs["update"]["$set"]


def wire(payload: dict, tz_aware: bool = True) -> dict:
    """Encode and decode exactly as the driver does for a client's codec."""
    raw = bson.encode(payload)
    return bson.decode(raw, CodecOptions(tz_aware=tz_aware))


# --------------------------------------------------------------------------
# The fix: datetimes reach the driver as datetimes, at every nesting level
# --------------------------------------------------------------------------


def test_top_level_datetime_stays_native():
    value = payload_of(build())["submitted_at"]
    assert isinstance(value, datetime.datetime)
    assert value == SUBMITTED


def test_top_level_datetime_is_not_a_string():
    """The direct regression guard — mode="json" produced a str here."""
    assert not isinstance(payload_of(build())["submitted_at"], str)


def test_datetime_in_nested_model_stays_native():
    nested = payload_of(build())["nested"]
    assert isinstance(nested, dict), "nested models still dump to plain dicts"
    assert isinstance(nested["at"], datetime.datetime)
    assert nested["at"] == NESTED


def test_datetime_in_list_of_models_stays_native():
    stamps = payload_of(build())["stamps"]
    assert isinstance(stamps[0]["at"], datetime.datetime)
    assert stamps[0]["at"] == IN_LIST


def test_datetime_in_plain_list_stays_native():
    times = payload_of(build())["times"]
    assert isinstance(times[0], datetime.datetime)
    assert times[0] == IN_LIST


def test_unset_datetime_stays_none():
    assert payload_of(build())["missing_at"] is None


def test_naive_datetime_is_passed_through_untouched():
    """The ORM does not invent a timezone; BSON reads it as UTC."""
    naive = datetime.datetime(2026, 1, 2, 3, 4, 5)
    value = payload_of(Doc(submitted_at=naive))["submitted_at"]
    assert value.tzinfo is None
    assert value == naive


# --------------------------------------------------------------------------
# The payload the driver receives must actually be BSON-encodable
# --------------------------------------------------------------------------


def test_payload_encodes_to_bson():
    payload = payload_of(build())
    assert isinstance(bson.encode(payload), bytes)


def test_datetime_survives_the_wire_as_a_date():
    doc = wire(payload_of(build()))
    assert isinstance(doc["submitted_at"], datetime.datetime)
    assert doc["nested"]["at"] == NESTED
    assert doc["stamps"][0]["at"] == IN_LIST
    assert doc["times"][0] == IN_LIST


def test_non_utc_offset_round_trips_to_the_same_instant():
    aware = datetime.datetime(2026, 1, 2, 5, 4, 5, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
    stored = wire(payload_of(Doc(submitted_at=aware)))["submitted_at"]
    assert stored == aware, "same instant"
    assert stored.utcoffset() == datetime.timedelta(0), "normalized to UTC by BSON"


def test_enums_still_reach_mongo_as_scalars():
    """mode="python" keeps enum *members*, but BSON encodes by their mixin."""
    payload = payload_of(build())
    assert isinstance(payload["colour"], Colour)
    doc = wire(payload)
    assert doc["colour"] == "red" and isinstance(doc["colour"], str)
    assert doc["level"] == 3 and isinstance(doc["level"], int)


def test_plain_fields_are_unchanged():
    doc = wire(payload_of(build()))
    assert doc["title"] == "t"
    assert doc["qty"] == 2
    assert doc["flag"] is True
    assert doc["tags"] == ["a", "b"]
    assert doc["nested"]["label"] == "n"


# --------------------------------------------------------------------------
# The read half: clients must return aware datetimes
# --------------------------------------------------------------------------


def test_sync_client_is_tz_aware():
    assert _build_sync_client(URI).codec_options.tz_aware is True


def test_async_client_is_tz_aware():
    assert _build_async_client(URI).codec_options.tz_aware is True


def test_a_tz_naive_client_would_break_comparison_against_now():
    """Why tz_aware is not optional.

    BSON stores UTC milliseconds with no offset, so a default client hands back
    naive datetimes. Entities compare timestamps against an aware now(), and
    that comparison raises — which is what tz_aware=True prevents.
    """
    payload = payload_of(build())
    naive = wire(payload, tz_aware=False)["submitted_at"]
    assert naive.tzinfo is None
    with pytest.raises(TypeError):
        naive < datetime.datetime.now(UTC)

    aware = wire(payload, tz_aware=True)["submitted_at"]
    assert aware.tzinfo is not None
    assert aware < datetime.datetime.now(UTC)


def test_full_cycle_through_load_entity_preserves_instants():
    """entity → $set → BSON → decode → entity, the way a save/find pair runs."""
    original = build()
    stored = wire(payload_of(original))
    loaded = Doc.load_entity(dict(stored))

    assert loaded.submitted_at == SUBMITTED
    assert loaded.submitted_at.tzinfo is not None
    assert loaded.nested.at == NESTED
    assert loaded.stamps[0].at == IN_LIST
    assert loaded.times[0] == IN_LIST
    assert loaded.colour is Colour.RED
    assert loaded.level is Level.HIGH
    assert loaded.tags == ["a", "b"]


# --------------------------------------------------------------------------
# Save mechanics that must not have shifted
# --------------------------------------------------------------------------


def test_updated_at_is_a_native_datetime():
    """Always was — _prepare_save assigns it after the dump, not through it."""
    update_kwargs, _ = build()._prepare_save()
    assert isinstance(update_kwargs["update"]["$set"]["updated_at"], datetime.datetime)


def test_created_at_is_only_set_on_insert():
    update_kwargs, _ = build()._prepare_save()
    update = update_kwargs["update"]
    assert "created_at" not in update["$set"]
    assert isinstance(update["$setOnInsert"]["created_at"], datetime.datetime)


def test_filter_and_upsert_are_unchanged():
    update_kwargs, _ = build()._prepare_save()
    assert update_kwargs["filter"] == {"_id": "doc-1"}
    assert update_kwargs["upsert"] is True
    assert update_kwargs["update"]["$set"]["_id"] == "doc-1"


def test_repeated_saves_are_stable():
    doc = build()
    first = payload_of(doc)["submitted_at"]
    second = payload_of(doc)["submitted_at"]
    assert first == second == SUBMITTED


# --------------------------------------------------------------------------
# Known limits of mode="python" — these fail loudly at save, never silently
# --------------------------------------------------------------------------

# mode="json" stringified these; mode="python" hands the driver the live object
# and BSON has no encoding for it. A model declaring one raises InvalidDocument
# on save rather than writing bad data, so the failure is impossible to miss.
UNSUPPORTED = {
    "date": datetime.date(2026, 1, 2),
    "time": datetime.time(3, 4, 5),
    "timedelta": datetime.timedelta(hours=2),
    "set": {"a"},
    "frozenset": frozenset(["a"]),
    "Decimal": decimal.Decimal("1.5"),
    "UUID": uuid.uuid4(),
}


@pytest.mark.parametrize("name", sorted(UNSUPPORTED))
def test_types_bson_cannot_encode_raise_rather_than_corrupt(name):
    with pytest.raises((bson.InvalidDocument, ValueError)):
        bson.encode({"f": UNSUPPORTED[name]})


def test_plain_enum_without_a_scalar_mixin_is_unsupported():
    class Plain(Enum):
        A = "a"

    with pytest.raises(bson.InvalidDocument):
        bson.encode({"f": Plain.A})


def test_microseconds_are_truncated_to_milliseconds():
    """BSON dates carry milliseconds; the ISO strings carried microseconds.

    Sub-millisecond precision is lost on write. Nothing in the codebase orders
    on it, but equality against a pre-save value needs a millisecond-aligned
    datetime — which is why this file's fixtures use microsecond=0.
    """
    precise = datetime.datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    stored = wire(payload_of(Doc(submitted_at=precise)))["submitted_at"]
    assert stored.microsecond == 123000
    assert stored != precise
