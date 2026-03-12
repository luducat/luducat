# Authentication Patterns

Plugins authenticate with external services in five ways. The pattern you
choose depends on the service's API design.

## Pattern 1: No Authentication

For public APIs or local-only plugins (platforms, some metadata providers).

```json
{"auth": {"type": "none"}}
```

```python
class PcgwProvider(AbstractMetadataProvider):
    def is_authenticated(self) -> bool:
        return True  # Always available

    async def authenticate(self) -> bool:
        return True  # Nothing to do
```

## Pattern 2: API Key

The user provides an API key through the settings dialog. The key is stored
in the system keyring.

```json
{
  "auth": {
    "type": "api_key",
    "fields": ["api_key"],
    "help_url": "https://example.com/api-keys",
    "help_text": "Get API Key"
  },
  "settings_schema": {
    "api_key": {
      "type": "string",
      "label": "API Key",
      "secret": true,
      "required": true
    }
  }
}
```

```python
class MyStore(AbstractGameStore):
    def is_authenticated(self) -> bool:
        return bool(self.get_credential("api_key"))

    async def authenticate(self) -> bool:
        api_key = self.get_credential("api_key")
        if not api_key:
            raise AuthenticationError("API key not configured")

        # Validate the key
        resp = self.http.get(
            "https://api.example.com/validate",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code != 200:
            raise AuthenticationError("Invalid API key")
        return True
```

### Storing Credentials

When the user enters a key in the settings dialog, save it:

```python
self.set_credential("api_key", user_input)
```

Retrieve it later:

```python
api_key = self.get_credential("api_key")
```

Delete it:

```python
self.delete_credential("api_key")
```

Credentials are stored in the system keyring (GNOME Keyring, KWallet, macOS
Keychain, Windows Credential Manager).

## Pattern 3: OAuth

For services with OAuth/OpenID flows. The plugin manages token refresh.

```json
{
  "auth": {
    "type": "oauth",
    "fields": ["client_id", "client_secret"],
    "optional": true,
    "help_url": "https://dev.example.com",
    "help_text": "Provide your own credentials"
  }
}
```

```python
class MyProvider(AbstractMetadataProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._token = None
        self._token_expires = 0

    def is_authenticated(self) -> bool:
        return self._token is not None and time.time() < self._token_expires

    async def authenticate(self) -> bool:
        client_id = self.get_credential("client_id")
        client_secret = self.get_credential("client_secret")
        if not client_id or not client_secret:
            raise AuthenticationError("OAuth credentials not configured")

        resp = self.http.post(
            "https://auth.example.com/oauth2/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + data["expires_in"] - 60
        return True
```

## Pattern 4: Browser Cookies

For services where the user is already logged in via their browser and no
public API exists.

```json
{
  "auth": {
    "type": "browser_cookies",
    "help_text": "Uses browser cookies from example.com"
  }
}
```

```python
from luducat.plugins.sdk.cookies import get_browser_cookie_manager

class MyStore(AbstractGameStore):
    def is_authenticated(self) -> bool:
        if not self.has_local_data_consent():
            return False
        cookie_mgr = get_browser_cookie_manager()
        if not cookie_mgr:
            return False
        cookies = cookie_mgr.get_cookies("example.com")
        return any(c.name == "session_token" for c in cookies)

    async def authenticate(self) -> bool:
        if not self.has_local_data_consent():
            raise AuthenticationError("Local data access not authorized")
        # Cookies are read-only; authentication happens in the browser
        if not self.is_authenticated():
            raise AuthenticationError(
                "Please log in to example.com in your browser"
            )
        return True
```

**Privacy requirement:** Always check `has_local_data_consent()` before
accessing browser cookies. The user must explicitly grant consent.

## Pattern 5: External Tool

For services that authenticate through an external CLI tool.

```json
{
  "auth": {
    "type": "external_tool",
    "tool_name": "legendary",
    "help_text": "Uses Legendary CLI for authentication"
  }
}
```

```python
import subprocess

class EpicStore(AbstractGameStore):
    def is_authenticated(self) -> bool:
        try:
            result = subprocess.run(
                ["legendary", "status"],
                capture_output=True, text=True, timeout=10,
            )
            return "Logged in" in result.stdout
        except FileNotFoundError:
            return False

    async def authenticate(self) -> bool:
        result = subprocess.run(
            ["legendary", "auth"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise AuthenticationError(f"Legendary auth failed: {result.stderr}")
        return True
```

## Credential Helpers Summary

Available on all plugin base classes (`AbstractGameStore`,
`AbstractMetadataProvider`):

| Method | Description |
|--------|-------------|
| `get_credential(key)` | Retrieve from keyring. Returns `str` or `None`. |
| `set_credential(key, value)` | Store in keyring. |
| `delete_credential(key)` | Remove from keyring. |

The keyring service name is `luducat.{plugin_name}` (configurable via
`credentials.keyring_service` in plugin.json).

## Gotchas

- **Never embed credentials in source.** API keys, tokens, secrets -- all go
  in the keyring via `set_credential()`.
- **Settings dialog handles key input.** The `settings_schema` with
  `"secret": true` renders a password field. The generic dialog saves it to
  the keyring automatically.
- **Privacy consent is per-session.** It's injected by the plugin manager from
  the global config. Don't cache it -- always call `has_local_data_consent()`.
