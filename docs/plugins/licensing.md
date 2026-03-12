# Plugin Licensing

## The Short Version

- Luducat core is **GPLv3**
- Plugins can use **any OSI-approved license** (MIT, BSD, Apache 2.0, GPLv2+, etc.)
- The Plugin SDK is the GPL boundary -- import from `luducat.plugins.sdk.*` and
  `luducat.plugins.base` freely without GPL obligations on your plugin

## How It Works

Luducat's license includes a **Plugin Exception Clause**:

> Third-party plugins that interface with Luducat exclusively through the
> documented plugin API (`luducat.plugins.base` and `luducat.plugins.sdk.*`),
> the theme/stylesheet system, or inter-process communication mechanisms are
> NOT considered derivative works of Luducat and may be distributed under any
> OSI-approved license.

This means your plugin's license is your choice, as long as you stay within the
SDK boundary.

## Safe Imports (Any License)

```python
# Base classes and dataclasses -- always safe
from luducat.plugins.base import (
    AbstractGameStore, StorePlugin,
    AbstractMetadataProvider, MetadataPlugin,
    AbstractPlatformProvider, PlatformPlugin,
    AbstractRunnerPlugin, RunnerPlugin,
    Game, EnrichmentData, MetadataSearchResult,
    ConfigAction, PluginMetadata, PluginType,
    PluginError, AuthenticationError, RateLimitError, NetworkError,
    CANONICAL_METADATA_FIELDS,
)

# SDK utilities -- always safe
from luducat.plugins.sdk.network import PluginHttpClient, is_online
from luducat.plugins.sdk.storage import PluginStorage
from luducat.plugins.sdk.config import get_data_dir, get_cache_dir
from luducat.plugins.sdk.json import loads, dumps
from luducat.plugins.sdk.datetime import utc_now, parse_release_date
from luducat.plugins.sdk.text import normalize_title
from luducat.plugins.sdk.ui import create_status_label, show_error
from luducat.plugins.sdk.constants import APP_VERSION, USER_AGENT
from luducat.plugins.sdk.cookies import get_browser_cookie_manager
from luducat.plugins.sdk.dialogs import set_status_property
```

## Forbidden Imports (GPL Boundary Violation)

```python
# NEVER do this in a third-party plugin
from luducat.core.database import Session, Game    # GPL!
from luducat.core.config import Config             # GPL!
from luducat.core.game_service import GameService   # GPL!
```

Direct imports from `luducat.core.*` make your plugin a derivative work of the
GPLv3 codebase. The import audit will **block your plugin from loading** if it
contains such imports.

**Bundled plugins** (shipped with luducat) are exempt from this restriction
since they're already GPLv3.

## Standard Library and Third-Party

Your plugin can freely use:
- Python standard library (`os`, `pathlib`, `sqlite3`, `json`, `re`, etc.)
- Third-party packages (`requests`, `aiohttp`, `beautifulsoup4`, etc.)
- PySide6/Qt (LGPL, dynamically linked)

Just declare dependencies in your `plugin.json`.

## Trademark

"Luducat" and the luducat logo are trademarks. The license grants software
rights, not trademark rights.

**Acceptable:**
- "MyPlugin -- a Luducat plugin for Battle.net"
- "Compatible with Luducat"

**Not acceptable:**
- "Luducat Plus"
- "Luducat Community Edition"

See the `LICENSE` file for full trademark terms.

## Practical Q&A

**Q: Can I sell a plugin?**
A: Yes, as long as it's under an OSI-approved license and interfaces only
through the SDK. Commercial plugins are welcome.

**Q: Can I keep my plugin closed-source?**
A: Yes, if it only imports from `luducat.plugins.base` and
`luducat.plugins.sdk.*`. The plugin exception allows any OSI license, including
permissive ones that don't require source distribution.

**Q: What if I need something that's only in `luducat.core`?**
A: File a feature request. If it's useful for plugins, we'll expose it through
the SDK. Don't reach into core -- the import audit will catch it, and your
plugin won't load.

**Q: Does contributing to luducat's core transfer copyright?**
A: No. Contributors retain copyright but grant a perpetual, royalty-free license
for use under GPLv3+. See `LICENSE` for full terms.

**Q: What license should I choose for my plugin?**
A: That's up to you. MIT and Apache 2.0 are common choices for permissive
plugins. GPLv2+ or GPLv3 if you want copyleft. The plugin generator defaults
to GPLv2+ but lets you choose any OSI license.
