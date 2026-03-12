# Error Handling

## Exception Hierarchy

```
PluginError                    Base exception for all plugin errors
    AuthenticationError        Auth failures (missing key, expired token, etc.)
    RateLimitError             API rate limit exceeded (429, 403, proactive)
    NetworkError               Network request failures
```

All exceptions are defined in `luducat.plugins.base`.

## Import

```python
from luducat.plugins.base import (
    PluginError,
    AuthenticationError,
    RateLimitError,
    NetworkError,
)
```

## When to Raise

### `AuthenticationError`

Raise when authentication is required but missing or invalid:

```python
async def authenticate(self) -> bool:
    api_key = self.get_credential("api_key")
    if not api_key:
        raise AuthenticationError("API key not configured")
    # ...

async def fetch_user_games(self, **kwargs):
    if not self.is_authenticated():
        raise AuthenticationError("Not authenticated")
    # ...
```

### `RateLimitError`

Raise when the API returns a 429 or 403 rate limit response, or when your
plugin proactively detects it's about to exceed a rate limit:

```python
if response.status_code == 429:
    retry_after = int(response.headers.get("Retry-After", 300))
    raise RateLimitError(
        message="Rate limit exceeded",
        wait_seconds=retry_after,
        reason="429",
    )

if response.status_code == 403 and "rate" in response.text.lower():
    raise RateLimitError(
        message="Forbidden (likely rate limit)",
        wait_seconds=300,
        reason="403",
    )
```

The sync system catches `RateLimitError` and handles retry, wait timers, and
UI notification. Don't sleep inside your plugin.

**Constructor:** `RateLimitError(message, wait_seconds=300, reason="429")`

| Parameter | Description |
|-----------|-------------|
| `message` | Human-readable error message |
| `wait_seconds` | How long to wait before retrying (seconds) |
| `reason` | Why the limit was hit: `"429"`, `"403"`, `"proactive"` |

### `NetworkError`

Raise on network failures (connection timeout, DNS resolution, etc.).
Import exception types from the SDK network module:

```python
from luducat.plugins.sdk.network import (
    RequestException,
    RequestTimeout,
    ConnectionError as RequestConnectionError,
)

try:
    resp = self.http.get(url, timeout=10)
except RequestTimeout:
    raise NetworkError(f"Request timed out: {url}")
except RequestConnectionError as e:
    raise NetworkError(f"Cannot reach {url}: {e}")
except RequestException as e:
    raise NetworkError(f"Request failed: {e}")
```

### `PluginError`

Raise for general plugin errors that don't fit the other categories:

```python
if not self.data_dir.exists():
    raise PluginError("Plugin data directory missing")
```

## When NOT to Raise

### During Batch Operations

When fetching metadata for many games, don't let one failure stop the batch.
Log the error and skip:

```python
async def fetch_game_metadata(self, app_ids, download_images=False):
    results = []
    for app_id in app_ids:
        try:
            game = await self._fetch_single_game(app_id)
            results.append(game)
        except Exception as e:
            logger.warning(f"Failed to fetch {app_id}: {e}")
            # Skip this game, continue with others
    return results
```

### During Enrichment

The `enrich_games()` default implementation already handles per-game errors.
If you override it, follow the same pattern.

## Retry Patterns

### Don't Retry Internally

Let the sync system handle retries. Raise the appropriate exception:

```python
# GOOD -- raise, let sync system handle it
if response.status_code == 429:
    raise RateLimitError(wait_seconds=int(response.headers.get("Retry-After", 60)))

# BAD -- sleeping inside the plugin
if response.status_code == 429:
    time.sleep(60)  # Blocks the sync thread
    response = self.http.get(url)
```

### Cancel Check

Long-running operations should check the cancel callback:

```python
async def fetch_user_games(self, status_callback=None, cancel_check=None, **kw):
    all_games = []
    for page in range(1, total_pages + 1):
        if cancel_check and cancel_check():
            break  # User requested cancel
        games = await self._fetch_page(page)
        all_games.extend(games)
        if status_callback:
            status_callback(f"Page {page}/{total_pages}")
    return all_games
```

## Gotchas

- **Don't catch `RateLimitError` in your own code** unless you're transforming
  it. The sync system needs to see it to handle cooldowns.
- **Don't `sys.exit()` on errors.** Raise an exception and let the caller
  decide how to handle it.
- **Log at the right level.** `logger.warning()` for recoverable issues,
  `logger.error()` for things that need attention, `logger.debug()` for
  verbose diagnostic info.
