# mongo-orm

A MongoDB ORM for Python

## Usage

### Defining an Entity

Use the `@entity` decorator to map a class to a MongoDB collection. Your class should inherit from `BaseEntity`.

```python
from mongo_orm import BaseEntity, entity

@entity("users")
class User(BaseEntity):
    name: str
    email: str
    
    # Define indexes
    __indexes__ = [
        {"keys": [("email", 1)], "unique": True},
        {"keys": [("name", 1)]}
    ]
```

### Index Management

The ORM can automatically manage your MongoDB indexes based on the `__indexes__` attribute. This behavior is controlled by the `MONGODB_INDEX_AUTOAPPLY` environment variable.

#### Configuration (`MONGODB_INDEX_AUTOAPPLY`)

- `never` (default): No automatic index management.
- `always`: Checks and applies indexes every time an entity class is initialized (on startup).
- `auto-lock`: Checks and applies indexes once. After a successful check, it writes a hash of the index to `mongo-orm.lock`. Subsequent startups will skip the check if the hash is present in the lock file.

#### Manual Index Application

You can also trigger index application manually for all registered entities:

```python
from mongo_orm.utils import apply_all_indexes

# Apply indexes using the current environment configuration
apply_all_indexes()

# Or force a specific mode
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