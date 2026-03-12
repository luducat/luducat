# Image Handling in Steam Scraper (v1.0.4+)

## ✅ What the Module Now Does

### Downloaded to Cache:

1. **Header/Capsule Image** (460x215px)
   - Source: `game.header_image` from API
   - Saved as: `cache/{appid}/header.jpg`
   - The main store capsule image

2. **Capsule Image Variant**
   - Source: `game.capsule_image` from API
   - Saved as: `cache/{appid}/capsule.jpg`
   - Alternative capsule format

3. **Background/Hero Image** (1920x1080px typically)
   - Source: `game.background_image` from API
   - Saved as: `cache/{appid}/background.jpg`
   - Large background image used on store page

4. **Logo**
   - Source: `game.logo_url` from API
   - Saved as: `cache/{appid}/logo.png` or `logo.jpg`
   - Game logo with transparency (usually PNG)

5. **Screenshots** (1920x1080px)
   - Source: `screenshots` array from API
   - Saved as: `cache/{appid}/{appid}_1.jpg`, `{appid}_2.jpg`, etc.
   - Carousel screenshots from store page
   - Metadata stored in `Image` table

### Stored in Database:

All image URLs are stored in the `Game` model:
- `header_image` - Header/capsule URL
- `capsule_image` - Capsule variant URL
- `background_image` - Background/hero URL
- `logo_url` - Logo URL

## Age Gate Handling

The module automatically handles Steam's age verification for mature content (18+) games:

### How It Works:

1. **Preventive Cookies**: Sets age verification cookies on initialization
   ```python
   birthtime=-473385600  # January 1, 1955
   mature_content=1
   wants_mature_content=1
   ```

2. **Detection**: Checks for age gate HTML patterns
   ```python
   if 'app_agegate' in html or 'Please enter your birth date' in html:
       # Age gate detected
   ```

3. **Bypass**: Auto-submits verification form if needed
   ```python
   POST /agecheckset/app/{appid}/
   Data: ageDay=1, ageMonth=January, ageYear=1955
   ```

4. **Retry**: Fetches page again after verification

### Games That Trigger Age Gates:
- Mature rated (18+) games
- Games with violence, sexual content
- Games with drug references
- Regional restrictions

### What You Get:

```python
from steam_scraper import SteamGameManager

manager = SteamGameManager()

# Works transparently even for mature games
game = manager.get_game(appid=730)  # CS:GO (violence)

# All images downloaded automatically
print(game.header_image)       # URL
print(game.background_image)   # URL
print(game.logo_url)          # URL

# Check cache directory
# cache/730/header.jpg
# cache/730/background.jpg
# cache/730/logo.png
# cache/730/730_1.jpg (screenshot 1)
# cache/730/730_2.jpg (screenshot 2)
# ...

manager.close()
```

## Complete Cache Structure

After fetching a game, your cache directory will contain:

```
cache/
└── {appid}/
    ├── header.jpg          ← Main capsule (460x215)
    ├── capsule.jpg         ← Alternative capsule
    ├── background.jpg      ← Hero/background image
    ├── logo.png            ← Game logo (PNG or JPG)
    ├── {appid}_1.jpg       ← Screenshot 1
    ├── {appid}_2.jpg       ← Screenshot 2
    ├── {appid}_3.jpg       ← Screenshot 3
    └── ...
```

## What Changed from v1.0.3

### Before (v1.0.3):
- ✅ Screenshots downloaded
- ✅ `header_image` URL stored
- ❌ Header image NOT downloaded
- ❌ No capsule, background, or logo handling
- ❌ Age gates blocked scraping

### After (v1.0.4):
- ✅ Screenshots downloaded
- ✅ All image URLs stored in database
- ✅ All images downloaded to cache
- ✅ Age gates handled automatically
- ✅ Complete image coverage

## Migration Notes

If you're upgrading from v1.0.3 or earlier:

1. **Database Migration**: Automatic
   - Schema upgrades from v1 to v2
   - New columns added: `capsule_image`, `background_image`, `logo_url`
   - Existing data preserved

2. **Re-fetch for Images**: Optional
   - Existing games won't have new images automatically
   - Use refresh command to download missing images:
   ```bash
   python cli.py refresh --all
   ```

3. **Cache Directory**: No changes needed
   - New images saved alongside existing screenshots
   - No conflicts with existing files

## API Response Example

What we get from Steam API:

```json
{
  "header_image": "https://cdn.../apps/440/header.jpg",
  "capsule_image": "https://cdn.../apps/440/capsule_231x87.jpg",
  "background": "https://cdn.../apps/440/page_bg_generated.jpg",
  "logo": "https://cdn.../apps/440/logo.png",
  "screenshots": [
    {"path_full": "https://cdn.../apps/440/ss_xxx.1920x1080.jpg"}
  ]
}
```

All of these are now stored and downloaded! ✅

## Troubleshooting

### Age Gate Still Appearing

If you encounter persistent age gates:
1. Check that cookies are being set (check `scraper.session.cookies`)
2. Verify the game's region restrictions
3. Some games may require Steam login (not supported)

### Missing Images

If some images aren't downloaded:
1. Check that the API returned URLs (`game.header_image`, etc.)
2. Some games may not have all image types
3. Use refresh command to retry: `python cli.py refresh --appid {appid}`

### Old Games Missing New Images

Games fetched before v1.0.4 won't have the new images. To fix:

```bash
# Refresh all games
python cli.py refresh --all

# Or specific game
python cli.py refresh --appid 440
```

This will download any missing header, capsule, background, and logo images.

## What the Module Currently Does

### ✅ Stored (URLs only, not downloaded):
- **header_image**: The main capsule/cover image URL (460x215px)
  - Example: `https://cdn.akamai.steamstatic.com/steam/apps/440/header.jpg`
  - Stored in: `Game.header_image` field in database
  - **Not downloaded** to cache

### ✅ Downloaded to Cache:
- **screenshots**: Carousel screenshots from store page
  - Full resolution (1920x1080px)
  - Stored in: `cache/{appid}/{appid}_1.jpg, {appid}_2.jpg, etc.`
  - Metadata in: `Image` table in database

### ❌ NOT Currently Handled:
The Steam API provides several other image types that we currently **do not store or download**:

1. **capsule_image**: Store capsule (different size than header)
2. **capsule_imagev5**: Alternative capsule format
3. **background**: Large background/hero image for store page
4. **background_raw**: Raw background image
5. **icon**: Small app icon (used in library)
6. **logo**: Game logo image

## What You Can Access Now

```python
from steam_scraper import SteamGameManager

manager = SteamGameManager()
game = manager.get_game(appid=440)

# ✅ Available: Header image URL (not downloaded)
print(game.header_image)
# Output: https://cdn.akamai.steamstatic.com/steam/apps/440/header.jpg

# ✅ Available: Screenshots (downloaded)
for img in game.images:
    print(f"cache/440/{img.filename}")
# Output: cache/440/440_1.jpg, cache/440/440_2.jpg, etc.

# ❌ NOT available: Hero/background images, icon, logo
# These are not stored or downloaded

manager.close()
```

## Steam API Image Types Explained

When you call the Steam API for appdetails, you get these image URLs:

```json
{
  "header_image": "https://.../apps/440/header.jpg",           // ✅ Stored (URL)
  "capsule_image": "https://.../apps/440/capsule_231x87.jpg",  // ❌ Not stored
  "capsule_imagev5": "https://.../apps/440/capsule_184x69.jpg",// ❌ Not stored  
  "background": "https://.../apps/440/page_bg_generated_v6b.jpg", // ❌ Not stored
  "background_raw": "https://.../apps/440/page.bg.jpg",        // ❌ Not stored
  "screenshots": [                                              // ✅ Downloaded
    {"path_full": "https://.../apps/440/ss_xxx.1920x1080.jpg"}
  ]
}
```

## Would You Like These Added?

I can extend the module to:

### Option 1: Store Additional Image URLs
Add database fields for:
- `capsule_image`
- `background_image`
- `icon_url`
- `logo_url`

These would be **stored as URLs only** (not downloaded), just like `header_image`.

### Option 2: Download Additional Images
Extend the image download functionality to also download:
- Header/capsule image → `cache/{appid}/header.jpg`
- Background/hero image → `cache/{appid}/background.jpg`
- Icon → `cache/{appid}/icon.jpg`
- Logo → `cache/{appid}/logo.jpg`

### Option 3: Both
Store URLs AND download the images to cache.

## Recommendation

**Option 3** is the most complete solution:

1. **Store URLs** - Always have the source reference
2. **Download images** - Have local copies for offline use
3. **Organize by type** - Clear naming: `header.jpg`, `background.jpg`, `icon.jpg`, `logo.jpg`
4. **Include in refresh** - The refresh command would also update these images

This would give you:
```
cache/
└── 440/
    ├── header.jpg          (460x215 - capsule)
    ├── background.jpg      (1920x1080 - hero image)
    ├── icon.jpg            (32x32 or 64x64)
    ├── logo.jpg            (variable size)
    ├── 440_1.jpg           (screenshot 1)
    ├── 440_2.jpg           (screenshot 2)
    └── ...
```

Would you like me to implement this? If so, which option do you prefer?
