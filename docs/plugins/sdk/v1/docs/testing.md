# Testing Plugins

## Project Structure

```
my_plugin/
    __init__.py
    plugin.json
    store.py
    tests/
        __init__.py
        test_store.py
        conftest.py
```

## pytest Setup

Install pytest and create a minimal `conftest.py`:

```python
# tests/conftest.py
import pytest
from pathlib import Path
from unittest.mock import MagicMock


@pytest.fixture
def plugin_dirs(tmp_path):
    """Create temporary plugin directories."""
    config_dir = tmp_path / "config"
    cache_dir = tmp_path / "cache"
    data_dir = tmp_path / "data"
    config_dir.mkdir()
    cache_dir.mkdir()
    data_dir.mkdir()
    return config_dir, cache_dir, data_dir
```

## Mocking PluginHttpClient

The HTTP client is injected, so mock it:

```python
@pytest.fixture
def mock_http():
    """Create a mock HTTP client."""
    http = MagicMock()

    # Configure response for specific URLs
    def mock_get(url, **kwargs):
        response = MagicMock()
        if "library" in url:
            response.status_code = 200
            response.json.return_value = {
                "games": [{"id": "1", "title": "Test Game"}]
            }
        else:
            response.status_code = 404
        return response

    http.get = mock_get
    return http


@pytest.fixture
def store(plugin_dirs, mock_http):
    """Create a store instance with mocked dependencies."""
    config_dir, cache_dir, data_dir = plugin_dirs
    store = MyStore(config_dir, cache_dir, data_dir)
    store.set_http_client(mock_http)
    store.set_credential_manager(MagicMock())
    store.set_settings({"api_key": "test_key"})
    return store
```

## Mocking PluginStorage

```python
from luducat.plugins.sdk.storage import PluginStorage

@pytest.fixture
def mock_storage(plugin_dirs):
    """Create a real PluginStorage with temp directories."""
    config_dir, cache_dir, data_dir = plugin_dirs
    return PluginStorage("my_plugin", config_dir, cache_dir, data_dir)
```

`PluginStorage` works fine with temporary directories. No mocking needed
unless you want to test error paths.

## Testing Store Plugins

### Test Authentication

```python
import pytest

@pytest.mark.asyncio
async def test_authenticate_success(store):
    store._credential_manager.get.return_value = "valid_key"
    result = await store.authenticate()
    assert result is True

@pytest.mark.asyncio
async def test_authenticate_no_key(store):
    store._credential_manager.get.return_value = None
    with pytest.raises(AuthenticationError):
        await store.authenticate()
```

### Test Fetch User Games

```python
@pytest.mark.asyncio
async def test_fetch_user_games(store):
    games = await store.fetch_user_games()
    assert isinstance(games, list)
    assert all(isinstance(g, str) for g in games)
```

### Test Game Metadata

```python
def test_get_game_metadata(store):
    metadata = store.get_game_metadata("123")
    assert metadata is not None
    assert "title" in metadata
```

## Testing Metadata Plugins

```python
@pytest.mark.asyncio
async def test_lookup_by_store_id(provider):
    provider_id = await provider.lookup_by_store_id("steam", "440")
    assert provider_id is not None

@pytest.mark.asyncio
async def test_get_enrichment(provider):
    enrichment = await provider.get_enrichment("12345")
    assert enrichment is not None
    assert enrichment.provider_name == "my_provider"
    assert isinstance(enrichment.genres, list)
```

## Testing with GameEntry

If your tests interact with the game cache, use `GameEntry`:

```python
from luducat.core.game_entry import GameEntry

game = GameEntry(
    id=1,
    title="Test Game",
    normalized_title="test game",
    primary_store="steam",
    stores=["steam"],
)
```

## Running Tests

```bash
# From your plugin directory
pytest tests/ -v

# With coverage
pytest tests/ --cov=. --cov-report=term-missing

# Run async tests (requires pytest-asyncio)
pip install pytest-asyncio
pytest tests/ -v
```

## Test Conventions

1. **One test file per source file.** `test_store.py` tests `store.py`.
2. **Use fixtures for common setup.** Plugin dirs, mock HTTP, mock credentials.
3. **Test the contract, not internals.** Focus on the abstract method
   implementations, not private helper functions.
4. **Mock external dependencies.** HTTP client, credential manager, filesystem.
5. **Test error paths.** Missing credentials, network failures, malformed
   responses.
6. **Use `tmp_path` for file operations.** pytest provides a temporary
   directory fixture. PluginStorage works directly with it.

## Gotchas

- **`pytest-asyncio` is required** for testing `async` methods. Install it:
  `pip install pytest-asyncio`.
- **Don't test against live APIs** in unit tests. Mock the HTTP client.
  Integration tests against live APIs should be in a separate directory and
  marked with `@pytest.mark.integration`.
- **GameEntry, not dict.** If your code interacts with `_games_cache`, entries
  must be `GameEntry` instances, not plain dicts.
