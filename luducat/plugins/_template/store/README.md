# Store Plugin Template

This is a template for creating new store plugins for luducat.

## Quick Start

1. Copy this `_template/store/` directory to a new directory with your store's name:
   ```bash
   cp -r _template/store/ epic/
   ```

2. Update `plugin.json`:
   - Change `name` to your store's identifier (lowercase, no spaces)
   - Change `display_name` to your store's display name
   - Update `store_class` to match your class name
   - Add any required dependencies to `packages`
   - Define your settings in `settings_schema`

3. Rename the class in `store.py`:
   - Rename `TemplateStore` to `YourStore` (e.g., `EpicStore`)
   - Update `__init__.py` imports

4. Implement required methods:
   - `store_name` (property)
   - `display_name` (property)
   - `is_available()`
   - `is_authenticated()`
   - `fetch_user_games()`
   - `fetch_game_metadata()`
   - `launch_game()`
   - `get_database_path()`

5. Implement optional methods as needed:
   - `get_auth_status()` - for plugin config UI
   - `get_store_page_url()`
   - `get_game_metadata()`, `get_games_metadata_bulk()`
   - `get_game_description()` - lazy loading for detail view
   - `download_game_images()` - lazy image downloading
   - `get_screenshots_for_app()`
   - `on_enable()`, `on_disable()`, `on_sync_complete()`, `close()`

## File Structure

```
your_store/
├── __init__.py       # Exports your store class
├── plugin.json       # Plugin metadata and settings
├── store.py          # Main store implementation
├── database.py       # Optional: local database for catalog
└── api.py            # Optional: API client for store's API
```

## Constructor Signature

**IMPORTANT**: The constructor MUST match the base class signature:

```python
def __init__(self, config_dir: Path, cache_dir: Path, data_dir: Path):
    super().__init__(config_dir, cache_dir, data_dir)
```

The three directories are:
- `config_dir`: Plugin config (~/.config/luducat/plugins/{name}/)
- `cache_dir`: Plugin cache (~/.cache/luducat/plugins/{name}/)
- `data_dir`: Plugin data (~/.local/share/luducat/plugins-data/{name}/)

## Required Methods Checklist

- [ ] `store_name` - Unique identifier (e.g., "epic")
- [ ] `display_name` - Human-readable name (e.g., "Epic Games")
- [ ] `is_available()` - Can this store be used on this system?
- [ ] `is_authenticated()` - Is the user logged in?
- [ ] `fetch_user_games()` - Get list of owned game IDs
- [ ] `fetch_game_metadata()` - Get details for games
- [ ] `launch_game()` - Launch a game via native launcher
- [ ] `get_database_path()` - Path to plugin's catalog DB

## Recommended Methods

- [ ] `get_auth_status()` - For plugin config dialog status display
- [ ] `get_game_description()` - Lazy load descriptions for detail view
- [ ] `download_game_images()` - Lazy download images when needed
- [ ] `on_enable()` / `on_disable()` - Lifecycle hooks
- [ ] `on_sync_complete()` - Post-sync cleanup
- [ ] `close()` - Cleanup on shutdown

## Authentication Patterns

### API Key Based (like Steam)
```python
def is_authenticated(self) -> bool:
    api_key = self.get_credential("api_key")
    return bool(api_key)

def get_auth_status(self) -> tuple:
    if not self.is_authenticated():
        return False, "API key not configured"
    return True, "Connected"
```

### Browser Cookie Based (like GOG)
```python
def is_authenticated(self) -> bool:
    cookie = self.get_credential("auth_cookie")
    return bool(cookie)

def get_login_config(self):
    from luducat.ui.dialogs.oauth_dialog import BrowserLoginConfig
    return BrowserLoginConfig(
        name=self.display_name,
        login_url="https://store.example.com/login",
        cookie_domain=".example.com",
        required_cookie="auth_token",
    )
```

### OAuth Flow
```python
async def authenticate(self) -> bool:
    # Start OAuth flow
    auth_url = "https://store.example.com/oauth/authorize"
    # ... handle OAuth callback
    self.set_credential("access_token", token)
    self.set_credential("refresh_token", refresh)
    return True
```

## Launcher Detection Pattern

For stores with multiple possible launchers (like GOG with Heroic vs Galaxy):

```python
def _detect_launcher(self) -> Optional[Dict[str, Any]]:
    """Detect which launcher to use, with caching"""
    if self._detected_launcher is not None:
        return self._detected_launcher

    if sys.platform == "win32":
        launcher = self._detect_launcher_windows()
    elif sys.platform == "darwin":
        launcher = self._detect_launcher_macos()
    else:
        launcher = self._detect_launcher_linux()

    self._detected_launcher = launcher
    return launcher

def _detect_launcher_linux(self) -> Optional[Dict[str, Any]]:
    # Check Flatpak
    flatpak_path = Path.home() / ".var" / "app" / "com.example.Launcher"
    if flatpak_path.exists():
        return {"type": "launcher", "path": flatpak_path, "name": "Launcher (Flatpak)"}

    # Check AppImage
    for appimage in Path.home().glob("Applications/Launcher*.AppImage"):
        return {"type": "launcher", "path": appimage, "name": "Launcher (AppImage)"}

    return None

def launch_game(self, app_id: str) -> bool:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    launcher = self._detect_launcher()
    if launcher:
        url = f"launcher://launch/{app_id}"
        return QDesktopServices.openUrl(QUrl(url))

    # Fallback to store page
    url = self.get_store_page_url(app_id)
    return QDesktopServices.openUrl(QUrl(url))
```

## Async/Await Pattern

Use `asyncio.to_thread()` for blocking I/O:

```python
import asyncio

async def fetch_user_games(self, status_callback=None) -> List[str]:
    # Run blocking database query in thread pool
    def query_db():
        db = self._get_db()
        return db.get_all_app_ids()

    app_ids = await asyncio.to_thread(query_db)
    return app_ids
```

## Tips

1. **Version bumping**: Always bump version in `plugin.json` when making changes
2. **Logging**: Use `logger.info()` for important events, `logger.debug()` for details
3. **Error handling**: Raise `PluginError` for recoverable errors
4. **Settings**: Access via `self.get_setting("key")` and `self.get_credential("key")`
5. **Paths**: Use `self.data_dir` for databases, `self.cache_dir` for cached files
6. **Cross-platform**: Use `pathlib.Path`, never hardcode path separators
7. **SQLAlchemy queries**: Use `selectinload` instead of `joinedload` for one-to-many
   relationships to avoid duplicate parent objects:
   ```python
   from sqlalchemy.orm import selectinload

   # CORRECT - selectinload performs separate queries, no duplicates
   games = session.query(Game).options(selectinload(Game.images)).all()

   # WRONG - joinedload causes duplicate Game objects (one per image)
   from sqlalchemy.orm import joinedload
   games = session.query(Game).options(joinedload(Game.images)).all()
   ```

## Database Pattern

Use SQLAlchemy for your catalog database:

```python
from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

class GameModel(Base):
    __tablename__ = 'games'
    id = Column(Integer, primary_key=True)
    app_id = Column(String(64), unique=True, index=True)
    title = Column(String(256))
    description = Column(Text)
    # ... other fields

class YourDatabase:
    def __init__(self, db_path: Path):
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
```

## Reference Plugins

Study these existing plugins for patterns:
- `steam/store.py` - API key auth, comprehensive metadata, family sharing
- `gog/store.py` - Browser cookie auth, OAuth flow, launcher detection (Heroic/Galaxy)
