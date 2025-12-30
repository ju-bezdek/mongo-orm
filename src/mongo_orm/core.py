import asyncio
import datetime
import inspect
import json
import logging
from collections import deque
import os
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Generator,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    TypedDict,
    Union,
)
from uuid import NAMESPACE_URL, uuid4, uuid5
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
from pydantic import BaseModel, Field, PrivateAttr
from pymongo.collection import Collection
from pymongo.database import Database as MongoDatabase
from pymongo.mongo_client import MongoClient
from pymongo import UpdateOne, InsertOne


class Config:
    @staticmethod
    def MONGODB_URI():
        return os.environ.get("MONGODB_URI", None)

    @staticmethod
    def MONGO_DATABASE_NAME():
        return os.environ.get("MONGO_DATABASE_NAME", None)

    @staticmethod
    def MONGODB_INDEX_AUTOAPPLY():
        return os.environ.get("MONGODB_INDEX_AUTOAPPLY", "never")


def encode_tenant_id(tenant_id, id):
    return str(uuid5(NAMESPACE_URL, f"{id}/{tenant_id}"))


class SaveResult(TypedDict):
    updated: bool
    inserted: bool


class Repository:
    all_entities: List[Type["BaseEntity"]] = []

    def __init__(self, mongo_db: MongoDatabase = None) -> None:
        if mongo_db:
            self.mongo_db = mongo_db
        else:

            self.mongo_client = MongoClient(
                Config.MONGODB_URI(), tlsAllowInvalidCertificates=True
            )
            self.async_mongo_client = AsyncIOMotorClient(
                Config.MONGODB_URI(), tls=True, tlsAllowInvalidCertificates=True
            )
            self.mongo_client.admin.command("ping")
            self.mongo_db = self.mongo_client[str(Config.MONGO_DATABASE_NAME())]
            self.async_mongo_db = self.async_mongo_client[
                str(Config.MONGO_DATABASE_NAME())
            ]


REPOSITORY = Repository()


T = TypeVar("T", bound="BaseEntity")
Keys = Union[
    str,
    List[Union[str, Tuple[str, int]]],
    Dict[str, int],
]


class IndexSpec(TypedDict, total=False):
    """
    Typed dict describing a MongoDB index.

    Examples:
      {"keys": "email", "unique": True}
      {"keys": [("tenant_id", 1), ("created_at", -1)], "name": "tenant_created_idx"}
      {"keys": {"email": 1}, "unique": True}
    """

    keys: Keys
    # Optional index options
    unique: bool
    sparse: bool
    background: bool
    name: str
    expireAfterSeconds: int
    expire_after_seconds: int  # alias
    partialFilterExpression: Dict[str, Any]
    weights: Dict[str, int]


class BaseEntity(BaseModel):
    id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="Unique identifier for the entity",
    )
    tenant_id: str = "-"
    _is_new: bool = PrivateAttr(True)
    _is_deleted: bool = PrivateAttr(False)
    _loaded_at: datetime.datetime | None = PrivateAttr(None)
    _tracked_fields_state: dict | None = PrivateAttr(None)

    __indexes__: Optional[List[IndexSpec]] = None

    def __init__(self, **data) -> None:
        if not hasattr(self.__class__, "collection"):
            raise Exception(
                "collection is not defined... did you forget to decorate the class with @metadata_entity?"
            )

        super().__init__(**data)

    def _get_save_callbacks(
        self,
    ) -> list[Callable[["BaseEntity"], None | Awaitable[None]]]:
        return getattr(self.__class__, "_save_callbacks", [])

    async def _handle_save_callbacks(self):
        awaitable_results = [
            r
            for hook in self._get_save_callbacks()
            if (r := hook(self)) and inspect.isawaitable(r)
        ]
        if awaitable_results:
            results = await asyncio.gather(*awaitable_results, return_exceptions=True)
            for e in results:
                if isinstance(e, Exception):
                    logging.error(
                        f"Error executing hook for {self.__class__.__name__}: {e}"
                    )

    @classmethod
    async def bulk_save(cls, entities: List["BaseEntity"], skip_callbacks=False):
        if not entities:
            return
        save_payloads = []
        callbacks = []
        results = []
        for entity in entities:
            update_kwargs, callback = entity._prepare_save()

            save_payloads.append(UpdateOne(**update_kwargs))
            callbacks.append(callback)

        collection = cls.get_async_collection()
        await collection.bulk_write(save_payloads)

    @classmethod
    def register_save_callback(
        cls, callback: Callable[["BaseEntity"], None | Awaitable[None]]
    ):
        callbacks = getattr(cls, "_save_callbacks", None)
        if not callbacks:
            callbacks = []
            setattr(cls, "_save_callbacks", callbacks)
        if callback not in callbacks:
            callbacks.append(callback)

    def _get_self_collection(self) -> Collection:
        return self.collection

    @classmethod
    def get_collection(cls) -> Collection:
        collection = None
        _cls = cls
        while _cls and collection is None:
            collection = getattr(cls, "collection", None)
            if collection is not None:
                return collection
            _cls = _cls.__base__ if issubclass(_cls.__base__, BaseEntity) else None

    @classmethod
    def get_async_collection(cls, force_refresh=True) -> AsyncIOMotorCollection:
        if cls.acollection is None or force_refresh:
            # if the collection has not been initialized in wrong event loop, it needs to be reinitialized
            mongo_client = AsyncIOMotorClient(Config.MONGODB_URI())
            db = mongo_client[str(Config.MONGO_DATABASE_NAME())]
            cls.acollection = db[cls.__collection_name__]

        return cls.acollection

    def get_id(self) -> str:
        return self.id

    def save(self):
        update_kwargs, callback = self._prepare_save()
        res = self.get_collection().update_one(**update_kwargs)
        callback(res)
        save_res = SaveResult(
            updated=res.modified_count > 0, inserted=res.upserted_id is not None
        )
        self._after_save(save_res)
        return self

    def _after_save(self, save_res: SaveResult = None):
        pass

    async def asave(self):
        update_kwargs, callback = self._prepare_save()
        try:
            res = await self.get_async_collection().update_one(**update_kwargs)
        except RuntimeError as e:
            res = await self.get_async_collection(force_refresh=True).update_one(
                **update_kwargs
            )
        callback(res)
        save_res = SaveResult(
            updated=res.modified_count > 0, inserted=res.upserted_id is not None
        )

        self._after_save(save_res)
        return self

    def _prepare_save(self):
        _exclude_fields = []
        payload = self.model_dump(mode="json", exclude=_exclude_fields)
        _id = payload.pop("id", None) or self.get_id()
        if not _id:
            raise Exception(
                "id is not defined. Please generate an id before saving or override get_id() method"
            )
        if hasattr(self, "id"):
            self.id = _id
        payload["_id"] = _id
        payload.pop("created_at", None)
        payload["updated_at"] = datetime.datetime.now(datetime.UTC)
        was_new = self._is_new
        self._is_new = False
        filter_extra = {}
        if getattr(self.__class__, "version_field", None):
            ver_id = str(uuid4())
            if getattr(self, self.__class__.version_field):
                filter_extra[self.__class__.version_field] = getattr(
                    self, self.__class__.version_field
                )
            setattr(self, self.__class__.version_field, ver_id)
            payload[self.__class__.version_field] = ver_id

        update_args = {
            "filter": {"_id": _id, **filter_extra},
            "update": {
                "$set": payload,
                "$setOnInsert": {"created_at": payload["updated_at"]},
            },
            "upsert": True,
        }

        def callback(res, _was_new=was_new):
            if hasattr(self, "updated_at"):
                setattr(self, "updated_at", payload["updated_at"])
            if res.modified_count == 0 and not _was_new:

                if getattr(self.__class__, "version_field", None):
                    raise Exception(
                        f"Save failed for {self.__class__.__name__} with id {_id} and version {ver_id} as it was trying to overwrite a newer version"
                    )
                else:
                    logging.warning(
                        f"No update for {self.__class__.__name__} with id {_id}"
                    )

            if res.upserted_id and hasattr(self, "created_at"):
                setattr(self, "created_at", payload["updated_at"])

        return update_args, callback

    def delete(self, ignore_not_found: bool = False):
        if hasattr(self, "id"):
            _id = self.id
        else:
            _id = self.get_id()
        self._is_new = True
        self._is_deleted = True
        res = self.get_collection().delete_one({"_id": _id})
        if res.deleted_count == 0:
            if ignore_not_found:
                logging.warning(
                    f"Entity {self.__class__.__name__} with id {_id} not found for deletion"
                )
            else:
                raise Exception(
                    f"Entity {self.__class__.__name__} with id {_id} not found for deletion"
                )

    @classmethod
    def delete_by_id(cls, id, tenant_id: str):

        filter = {"_id": id}
        if tenant_id != "*":
            filter["tenant_id"] = tenant_id

        return cls.collection.delete_one(filter).deleted_count == 1

    @classmethod
    def get(
        cls: Type[T],
        id: str,
        tenant_id: str,
        namespace: str = None,
        raise_not_found: bool = False,
    ) -> T | None:
        filter = {"_id": id}
        if tenant_id != "*":
            filter["tenant_id"] = tenant_id
        if namespace:
            filter["namespace"] = namespace
        data = cls.collection.find_one(filter)
        if not data and raise_not_found:
            raise Exception(
                f"Entity {cls.__name__} with id {id} not found for tenant {tenant_id}"
            )
        res: T = cls.load_entity(data) if data else None
        return res

    @classmethod
    async def aget(
        cls: Type[T],
        id: str,
        tenant_id: str,
        namespace: str = None,
        raise_not_found: bool = False,
    ) -> T | None:
        filter = {"_id": id}
        if tenant_id != "*":
            filter["tenant_id"] = tenant_id
        if namespace:
            filter["namespace"] = namespace

        data = await cls.get_async_collection().find_one(filter)

        if not data and raise_not_found:
            raise Exception(
                f"Entity {cls.__name__} with id {id} not found for tenant {tenant_id}"
            )
        res: T = cls.load_entity(data) if data else None
        return res

    @classmethod
    def load_entity(cls, raw_data: dict):
        if cls.has_id:
            raw_data["id"] = raw_data.pop("_id")
        elif "_id" in raw_data:
            del raw_data["_id"]

        if hasattr(cls, "parse_subclass"):
            entity = cls.parse_subclass(raw_data)
        else:
            entity = cls(**raw_data)  # cls.delete_by_id(raw_data["id"],"*")
        entity._is_new = False

        entity._loaded_at = datetime.datetime.now(datetime.UTC)
        return entity

    @classmethod
    def count(cls, tenant_id: str, filter: dict = None, limit=None) -> int:

        if not filter:
            _filter = {}
        else:
            _filter = {**filter}
        if tenant_id != "*":
            _filter["tenant_id"] = tenant_id

        extra = {}
        if limit:
            extra["limit"] = limit

        return cls.collection.count_documents(_filter, **extra)

    @classmethod
    async def acount(cls, tenant_id: str, filter: dict = None, limit=None) -> int:

        if not filter:
            _filter = {}
        else:
            _filter = {**filter}
        if tenant_id != "*":
            _filter["tenant_id"] = tenant_id

        extra = {}
        if limit:
            extra["limit"] = limit

        return await cls.get_async_collection().count_documents(_filter, **extra)

    @classmethod
    async def afind(
        cls: Type[T],
        tenant_id: str,
        namespace: str = None,
        skip: int = 0,
        limit: int = 100,
        order_by: str | list[str] = None,
        filter: dict = None,
        **additional_filters,
    ) -> List[T]:
        if not filter:
            filter = {}
        if tenant_id != "*":
            filter.update({"tenant_id": tenant_id, **additional_filters})

        filter.update(additional_filters)
        if "id" in filter:
            filter["_id"] = filter.pop("id")
        if namespace:
            filter["namespace"] = namespace
        query = cls.get_async_collection().find(filter)
        if order_by:
            if isinstance(order_by, str):
                order_by = [order_by]
            for ob_key in order_by:
                if ob_key.startswith("-"):
                    sort_order = -1
                    ob_key = ob_key[1:]
                else:
                    sort_order = 1
                query = query.sort([(ob_key, sort_order)])
        raw_items = await query.skip(skip).limit(limit).to_list(limit)
        res: List[T] = []
        for raw_item in raw_items:
            try:
                entity = cls.load_entity(raw_item)
                res.append(entity)
            except Exception as e:
                logging.error(f"Error loading entity {cls.__name__}  {raw_item}: {e}")
        return res

    @classmethod
    def scroll_pages(
        cls: Type[T],
        tenant_id: str,
        filter: dict = None,
        order_by: str = None,
        page_size: int = 100,
    ) -> Iterable[List[T]]:
        page = 0
        while True:
            items = cls.find(
                tenant_id,
                filter=filter,
                skip=page * page_size,
                order_by=order_by,
                limit=page_size,
            )
            if not items:
                break
            yield items
            page += 1

    @classmethod
    async def ascroll_pages(
        cls: Type[T],
        tenant_id: str,
        filter: dict = None,
        order_by: str = None,
        page_size: int = 100,
    ) -> AsyncIterator[List[T]]:
        page = 0
        while True:
            items = await cls.afind(
                tenant_id,
                filter=filter,
                skip=page * page_size,
                order_by=order_by,
                limit=page_size,
            )
            if not items:
                break
            yield items
            page += 1

    @classmethod
    def find(
        cls: Type[T],
        tenant_id: str,
        namespace: str = None,
        skip: int = 0,
        limit: int | None = 100,
        order_by: str | list[str] = None,
        filter: dict = None,
        **additional_filters,
    ) -> List[T]:
        if not filter:
            filter = {}
        else:
            filter = {**filter}
        if tenant_id != "*":
            filter.update({"tenant_id": tenant_id, **additional_filters})

        filter.update(additional_filters)
        if "id" in filter:
            filter["_id"] = filter.pop("id")
        if namespace:
            filter["namespace"] = namespace
        query = cls.get_collection().find(filter)
        if order_by:
            if isinstance(order_by, str):
                order_by = [order_by]
            for ob_key in order_by:
                if ob_key.startswith("-"):
                    sort_order = -1
                    ob_key = ob_key[1:]
                else:
                    sort_order = 1
                query = query.sort([(ob_key, sort_order)])

        if limit is None:
            limit = 100

        raw_items = list(query.skip(skip or 0).limit(limit or 100))
        res: List[T] = []
        for raw_item in raw_items:
            try:
                entity = cls.load_entity(raw_item)
                res.append(entity)
            except Exception as e:
                logging.error(f"Error loading entity {cls.__name__}  {raw_item}: {e}")
        return res

    @classmethod
    def find_first(cls, tenant_id, filter=None, order_by=None, **kwargs):
        kwargs.pop("limit", None)
        items = cls.find(tenant_id, filter=filter, order_by=order_by, limit=1, **kwargs)
        return items[0] if items else None

    @classmethod
    async def afind_first(cls, tenant_id, filter=None, order_by=None, **kwargs):
        items = await cls.afind(
            tenant_id, filter=filter, order_by=order_by, limit=1, **kwargs
        )
        return items[0] if items else None

    def update(
        self, auto_save: bool = False, raise_on_missing: bool = True, **to_update
    ):
        for k, v in to_update.items():
            if hasattr(self, k):
                setattr(self, k, v)
            elif raise_on_missing:
                raise ValueError(f"Attribute {k} doesn't exist on {self.__class__}")
        if auto_save:
            self.save()
        return self

    async def aupdate(
        self, auto_save: bool = False, raise_on_missing: bool = True, **to_update
    ):
        self.update(auto_save=False, raise_on_missing=raise_on_missing, **to_update)
        if auto_save:
            await self.asave()
        return self

    # def __delete__(self):
    #     if self._is_new:
    #         self.save()


def entity(
    collection_name: str, version_field: str = None, tracked_fields: List[str] = None
):
    def decorator(cls: Type[T]) -> Type[T]:
        if Config.MONGODB_URI() is None:
            raise Exception(
                "MONGODB_URI is not set in the environment variables. Please set it to connect to MongoDB."
            )
        if Config.MONGO_DATABASE_NAME() is None:
            raise Exception(
                "MONGO_DATABASE_NAME is not set in the environment variables. Please set it to connect to MongoDB."
            )
        cls.__collection_name__ = collection_name
        cls.collection = REPOSITORY.mongo_db[collection_name]

        cls.acollection = None
        if "id" in cls.model_fields:
            cls.has_id = True
        else:
            cls.has_id = False

        cls.version_field = version_field

        if cls not in REPOSITORY.all_entities:
            REPOSITORY.all_entities.append(cls)

        from .utils import apply_indexes

        apply_indexes(cls)

        return cls

    return decorator
