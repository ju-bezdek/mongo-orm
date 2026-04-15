# mongo-orm

A MongoDB ORM for Python

## Installation

```
pip install mongo-entity-orm
```

or

```
uv add mongo-entity-orm
```

## Configuration

Set the following environment variables before using the ORM:

| Variable | Description |
|---|---|
| `MONGODB_URI` | MongoDB connection string |
| `MONGO_DATABASE_NAME` | Name of the database |

## Usage

### Defining an Entity

Use the `@entity` decorator to map a class to a MongoDB collection. Your class should inherit from `BaseEntity`.

```python
from mongo_orm import BaseEntity, entity

@entity(collection_name="users")
class User(BaseEntity):
    name: str
    email: str
```

Every entity automatically has an `id` field (UUID string) and a `tenant_id` field (defaults to `"-"`).

### CRUD Operations

All operations are available in both async (`a`-prefixed) and sync variants.

#### Get by ID

```python
# async
user = await User.aget(id=user_id, tenant_id=tenant_id)

# sync
user = User.get(id=user_id, tenant_id=tenant_id)
```

Pass `raise_not_found=True` to raise an exception instead of returning `None` when the entity is not found.

#### Save

```python
user = User(name="Alice", email="alice@example.com", tenant_id="acme")

# async
await user.asave()

# sync
user.save()
```

`save()` / `asave()` performs an upsert based on `id`, so it works for both creating and updating.

#### Update fields

```python
# Update in-memory only
user.update(name="Bob")

# Update and immediately save (async)
await user.aupdate(name="Bob", auto_save=True)

# Update and immediately save (sync)
user.update(name="Bob", auto_save=True)
```

#### Delete

```python
# Delete an entity instance
user.delete()

# Delete by ID
User.delete_by_id(id=user_id, tenant_id=tenant_id)
```

Pass `ignore_not_found=True` to suppress an exception when the entity does not exist.

### Querying

#### Find (filter)

`filter` accepts a standard MongoDB query dict.

```python
# async
users = await User.afind(tenant_id=tenant_id, filter={"name": "Alice"})

# sync
users = User.find(tenant_id=tenant_id, filter={"name": "Alice"})
```

MongoDB operators work as expected:

```python
users = await User.afind(
    tenant_id=tenant_id,
    filter={"name": {"$in": ["Alice", "Bob"]}, "email": {"$regex": "@example.com$"}},
)
```

Use `tenant_id="*"` to query across all tenants.

#### Pagination

```python
users = await User.afind(tenant_id=tenant_id, filter={}, skip=0, limit=20)
```

#### Sorting

```python
# Ascending
users = await User.afind(tenant_id=tenant_id, order_by="name")

# Descending (prefix with "-")
users = await User.afind(tenant_id=tenant_id, order_by="-name")

# Multiple sort fields
users = await User.afind(tenant_id=tenant_id, order_by=["-created_at", "name"])
```

#### Find first

```python
user = await User.afind_first(tenant_id=tenant_id, filter={"email": "alice@example.com"})
```

Returns `None` if no match is found.

#### Count

```python
# async
total = await User.acount(tenant_id=tenant_id, filter={"name": "Alice"})

# sync
total = User.count(tenant_id=tenant_id, filter={"name": "Alice"})
```

#### Scroll pages (batch iteration)

```python
# async
async for page in User.ascroll_pages(tenant_id=tenant_id, page_size=50):
    for user in page:
        ...

# sync
for page in User.scroll_pages(tenant_id=tenant_id, page_size=50):
    for user in page:
        ...
```

#### Bulk save

```python
await User.bulk_save([user1, user2, user3])
```

### Index Management

Define indexes on the entity class using `__indexes__`:

```python
@entity(collection_name="users")
class User(BaseEntity):
    name: str
    email: str

    __indexes__ = [
        {"keys": [("email", 1)], "unique": True},
        {"keys": [("name", 1)]},
        {"keys": [("tenant_id", 1), ("name", 1)]},
    ]
```

Index management is controlled by the `MONGODB_INDEX_AUTOAPPLY` environment variable:

| Value | Behaviour |
|---|---|
| `never` (default) | No automatic index management |
| `always` | Checks and applies indexes on every startup |
| `auto-lock` | Applies indexes once; writes a hash to `mongo-orm.lock` and skips on subsequent startups |

#### Manual application

```python
from mongo_orm.utils import apply_all_indexes

# Respects MONGODB_INDEX_AUTOAPPLY
apply_all_indexes()

# Force a specific mode
apply_all_indexes(mode="always")
```

## Development

```bash
# Install dependencies
poetry install

# Run tests
poetry run pytest

# Format code
poetry run black .
poetry run isort .

# Type checking
poetry run mypy src/
```

## License

MIT