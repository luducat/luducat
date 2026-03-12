# SDK: Network (`sdk.network`)

HTTP client for plugins with domain enforcement and rate limiting.

## Import

```python
from luducat.plugins.sdk.network import PluginHttpClient, is_online
```

For exception handling and type annotations:

```python
from luducat.plugins.sdk.network import (
    PluginHttpClient,
    Response,
    RequestException,
    RequestTimeout,
    ConnectionError as RequestConnectionError,
    HTTPError,
)
```

## `is_online()`

Check if the network is available.

```python
if is_online():
    response = self.http.get("https://api.example.com/games")
```

Returns `True` if the network monitor detects connectivity, `False` otherwise.
Falls back to `True` if the network monitor isn't registered.

## `PluginHttpClient`

The sole HTTP interface for plugins. All requests go through the core
`NetworkManager` which enforces domain allowlists and rate limits declared in
your `plugin.json`.

### Usage

You don't instantiate `PluginHttpClient` yourself. It's injected by the plugin
manager and available as `self.http`:

```python
class MyStore(AbstractGameStore):
    async def fetch_user_games(self, **kwargs):
        response = self.http.get(
            "https://api.example.com/library",
            headers={"Authorization": f"Bearer {self.get_credential('token')}"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
```

### Methods

All methods accept the same keyword arguments as `requests.Session` methods
(`headers`, `params`, `data`, `json`, `timeout`, `allow_redirects`, etc.).

#### `get(url, **kwargs) -> requests.Response`

```python
resp = self.http.get("https://api.example.com/games", params={"page": 1})
```

#### `post(url, **kwargs) -> requests.Response`

```python
resp = self.http.post("https://api.example.com/auth", json={"key": api_key})
```

#### `put(url, **kwargs) -> requests.Response`

```python
resp = self.http.put("https://api.example.com/games/123", json=data)
```

#### `delete(url, **kwargs) -> requests.Response`

```python
resp = self.http.delete("https://api.example.com/games/123")
```

#### `head(url, **kwargs) -> requests.Response`

```python
resp = self.http.head("https://cdn.example.com/image.jpg")
content_length = resp.headers.get("Content-Length")
```

#### `request(method, url, **kwargs) -> requests.Response`

Generic method for any HTTP verb:

```python
resp = self.http.request("PATCH", "https://api.example.com/games/123", json=patch)
```

#### `session` (property)

Access the raw `requests.Session` for advanced use cases. This bypasses rate
limiting but still goes through the NetworkManager.

```python
session = self.http.session
session.headers.update({"X-Custom": "header"})
```

#### `get_stats() -> Dict[str, Dict[str, Any]]`

Get per-domain request statistics:

```python
stats = self.http.get_stats()
# {"api.example.com": {"count": 42, "bytes": 1048576, "last_request": "..."}}
```

#### `close()`

Release resources. Called automatically by the plugin manager on shutdown.

## Response and Exception Types

The SDK re-exports `requests` types so you never need a bare `import requests`.

### `Response`

All HTTP methods return a `Response` object. Use it for type annotations:

```python
from luducat.plugins.sdk.network import Response

def _check_response(self, response: Response) -> None:
    if response.status_code == 429:
        raise RateLimitError(wait_seconds=300)
```

### Exception Types

| SDK name | Original | Use for |
|----------|----------|---------|
| `RequestException` | `requests.RequestException` | Catch-all for any request failure |
| `RequestTimeout` | `requests.exceptions.Timeout` | Request timed out |
| `ConnectionError` | `requests.exceptions.ConnectionError` | DNS, connection refused, etc. |
| `HTTPError` | `requests.exceptions.HTTPError` | Non-2xx after `raise_for_status()` |

`RequestTimeout`, `ConnectionError`, and `HTTPError` are all subclasses of
`RequestException`, so a single `except RequestException` catches everything.

```python
from luducat.plugins.sdk.network import (
    RequestException,
    RequestTimeout,
    ConnectionError as RequestConnectionError,
)

try:
    resp = self.http.get(url, timeout=10)
    resp.raise_for_status()
except RequestTimeout as e:
    raise NetworkError(f"Timed out: {url}") from e
except RequestConnectionError as e:
    raise NetworkError(f"Cannot reach {url}: {e}") from e
except RequestException as e:
    raise NetworkError(f"Request failed: {e}") from e
```

**Alias tip:** `ConnectionError` shadows the Python builtin. Import it
as `RequestConnectionError` to avoid confusion.

## Domain Allowlists

Requests to domains not listed in your `plugin.json` `network.allowed_domains`
are blocked by the NetworkManager:

```json
{
  "network": {
    "allowed_domains": [
      "api.example.com",
      "cdn.example.com"
    ]
  }
}
```

A request to `https://evil.example.com/` will raise an error.

## Rate Limiting

Rate limits are declared per domain in `plugin.json`:

```json
{
  "network": {
    "rate_limits": {
      "api.example.com": {"requests": 5, "window": 1}
    }
  }
}
```

This limits requests to 5 per second for `api.example.com`. The NetworkManager
enforces this transparently -- your plugin doesn't need to implement its own
rate limiting.

## Gotchas

- **Always set a timeout.** The SDK does not add a default timeout. Requests
  without a timeout can hang indefinitely.
- **Don't `import requests`.** All types you need (`Response`, `RequestException`,
  `RequestTimeout`, etc.) are available from `sdk.network`. Bare requests
  bypass domain enforcement and rate limiting -- they will be flagged during
  review.
- **`self.http` is `None` before injection.** Don't access it during
  `__init__`. Use it in methods like `authenticate()`, `fetch_user_games()`,
  etc.
- **Rate limit errors.** If you hit a 429 response, raise `RateLimitError`
  with appropriate `wait_seconds`. The sync system handles retry and UI
  notification.
