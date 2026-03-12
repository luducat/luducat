# SDK: Text (`sdk.text`)

Title normalization for cross-store game deduplication.

## Import

```python
from luducat.plugins.sdk.text import normalize_title
```

## `normalize_title(title: str) -> str`

Normalize a game title into a canonical form for matching across stores.
Different stores use different formatting for the same game -- this function
strips those differences.

```python
normalize_title("The Elder Scrolls V: Skyrim")
# "elder scrolls 5 skyrim"

normalize_title("DOOM Eternal")
# "doom eternal"

normalize_title("The Witcher 3: Wild Hunt - Game of the Year Edition")
# "witcher 3 wild hunt"

normalize_title("FINAL FANTASY VII REMAKE")
# "final fantasy 7 remake"
```

### Pipeline

1. **Lowercase** -- case-insensitive matching
2. **`&` → `and`** -- normalize ampersands before punctuation strip
3. **Strip trademark symbols** -- `™`, `®`, `©`, `(TM)`, `(R)`, `(C)`
4. **Remove parenthesized years** -- `(2012)`, `(1998)`
5. **Strip edition suffixes** -- "Game of the Year Edition", "Deluxe Edition",
   "Remastered", etc. (trailing only)
6. **Remove leading articles** -- "The", "A", "An"
7. **Remove mid-title "the"** -- after `:` or `-` separators
8. **Convert Roman numerals** -- II-XX become Arabic (2-20)
9. **Remove punctuation + collapse whitespace**

### Deduplication Use Cases

When luducat imports games from multiple stores, it uses `normalize_title()`
to detect that "The Witcher 3: Wild Hunt - GOTY" (GOG) and
"The Witcher 3: Wild Hunt Game of the Year Edition" (Steam) are the same game.

Plugins should return raw titles from their APIs. The core handles
normalization during deduplication. You only need `normalize_title()` if your
plugin does its own cross-reference matching.

## Gotchas

- **Don't pre-normalize titles.** Return the original title from your store
  API. The core normalizes during deduplication.
- **Roman numeral conversion is limited to II-XX.** Single "I" is not
  converted because it's too ambiguous (could be the word "I").
- **Edition stripping is aggressive.** "Deluxe", "Premium", "Gold", "Enhanced",
  "Remastered", "Director's Cut" and similar suffixes are removed. This is
  intentional for deduplication but means normalized titles shouldn't be used
  for display.
