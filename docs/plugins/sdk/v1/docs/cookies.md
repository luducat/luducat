# SDK: Cookies (`sdk.cookies`)

Browser cookie access for plugins that authenticate via web sessions.

## Import

```python
from luducat.plugins.sdk.cookies import get_browser_cookie_manager
```

## `get_browser_cookie_manager() -> BrowserCookieManager`

Returns the shared `BrowserCookieManager` instance. Raises
`SdkNotInitializedError` if the registry hasn't been initialized (only
happens if called during module-level code before app startup).

```python
cookie_mgr = get_browser_cookie_manager()
if cookie_mgr:
    cookies = cookie_mgr.get_cookies("gog.com")
```

## Privacy Consent Gate

Browser cookie access requires user consent. The `BrowserCookieManager` checks
the privacy consent flag before returning cookies. If the user hasn't granted
consent, cookie retrieval returns empty results.

Check consent in your plugin:

```python
if not self.has_local_data_consent():
    raise AuthenticationError("Local data access not authorized by user")

cookie_mgr = get_browser_cookie_manager()
```

## When to Use

Use browser cookies when:
- The store doesn't have a public API (or the API requires OAuth that's
  impractical for a desktop app)
- The user is already logged into the store in their browser
- The authentication model is session-cookie-based (like GOG)

Declare the auth type in `plugin.json`:

```json
{
  "auth": {
    "type": "browser_cookies",
    "help_text": "Uses browser cookies from example.com"
  }
}
```

## Gotchas

- **Privacy-gated.** Always check `has_local_data_consent()` before accessing
  cookies.
- **Browser preference.** The user can select their preferred browser in
  Settings. The cookie manager respects this preference.
- **`None` before initialization.** The manager isn't available during
  module-level code. Call it inside methods.
