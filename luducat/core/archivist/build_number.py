# This file is part of luducat. License: GPL-3.0-or-later. Contact: luducat@trinity2k.net
"""Canonical GOG build-number extraction.

Single source of truth for reducing an installer filename or a version
string to one integer build number, used by the update check to compare
what is on disk against what GOG offers. The formula is Stefan's
gog-cleanup-archive.py extract_buildnum, but with the CONCATENATION
branch, not the additive sum that is currently the active branch in
that script:

  1. a parenthesized digit-only build id (NNNNN) wins; if several are
     present the LAST one is taken. "(64bit)" is not a build group -- it
     contains letters -- so it is ignored.
  2. otherwise a strictly four-part dotted version is concatenated into
     a number (2.2.20.2 -> 22202, NOT the sum 26), truncated to at most
     five digits from the left. This deliberately lands old dotted
     versions below the modern build-id range (80-90k) so a straight
     "newer wins" integer compare holds across the two numbering
     namespaces.
  3. otherwise 0.

Kept separate from audit.py's version_pseudo_build (which sums, matches
a looser dotted pattern, and drives display, not comparison) on
purpose: those are not interchangeable and must not be merged.
"""

import re
from typing import Optional

# Digit-only parenthesized build id. The capture group makes findall
# return the inner number; "(64bit)" fails the [0-9]+ requirement.
_PAREN_BUILD_RE = re.compile(r"\(([0-9]+)\)")

# Strictly four-part dotted version, e.g. 2.2.20.2. Three-part strings
# like 26.05.4 are deliberately not builds.
_DOTTED4_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")


def extract_buildnum(name: Optional[str]) -> int:
    """Integer build number for a GOG filename or version string.

    See the module docstring for the rules. Returns 0 for anything with
    no recoverable build (degenerate input, three-part versions,
    non-installer names).
    """
    if not name:
        return 0
    groups = _PAREN_BUILD_RE.findall(name)
    if groups:
        # Filter to plausible build IDs (4+ digits) so browser
        # duplicate suffixes like "(1)" don't shadow the real build.
        plausible = [g for g in groups if len(g) >= 4]
        if plausible:
            return int(plausible[-1])
        return int(groups[-1])
    m = _DOTTED4_RE.search(name)
    if not m:
        return 0
    digits = m.group(0).replace(".", "")[:5]
    return int(digits)
