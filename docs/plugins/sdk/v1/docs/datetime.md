# SDK: Datetime (`sdk.datetime`)

UTC timestamp handling and release date parsing.

## Import

```python
from luducat.plugins.sdk.datetime import (
    utc_now,
    utc_from_timestamp,
    parse_release_date,
    format_release_date,
)
```

## Functions

### `utc_now() -> datetime`

Return the current UTC time as a naive datetime (no timezone info):

```python
now = utc_now()
# datetime(2025, 6, 15, 14, 30, 0)
```

### `utc_from_timestamp(ts: float) -> datetime`

Convert a Unix timestamp to a naive UTC datetime:

```python
dt = utc_from_timestamp(1718451000)
# datetime(2025, 6, 15, 13, 30, 0)
```

### `parse_release_date(date_str) -> str | None`

Parse any release date string into ISO `YYYY-MM-DD` format. Handles many
formats from different stores and metadata sources:

```python
parse_release_date("Jun 15, 2025")       # "2025-06-15"
parse_release_date("2025-06-15")          # "2025-06-15"
parse_release_date("15/06/2025")          # "2025-06-15"
parse_release_date("June 2025")           # "2025-06-01"
parse_release_date("2025")               # "2025-01-01"
parse_release_date("TBD")                # None
parse_release_date("Coming Soon")         # None
parse_release_date(None)                  # None
```

Non-date placeholders like "TBD", "Coming Soon", "To be announced" return
`None`.

### `format_release_date(iso_str) -> str`

Format an ISO date string for display:

```python
format_release_date("2025-06-15")  # "Jun 15, 2025"
format_release_date(None)           # ""
format_release_date("")             # ""
```

## Gotchas

- **All datetimes are naive (no timezone).** The SDK uses UTC throughout but
  doesn't attach timezone info. This matches the convention used in the main
  database.
- **`release_date` can be a dict.** In the metadata system, `release_date`
  may be a per-platform dict (`{"windows": "2025-06-15", "linux": "2025-07-01"}`).
  Always check `isinstance(release_date, dict)` before passing to
  `parse_release_date()`.
- **Non-date strings return `None`.** The parser recognizes common placeholders
  and doesn't try to force them into dates.
