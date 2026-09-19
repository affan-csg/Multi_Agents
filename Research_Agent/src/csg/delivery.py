"""Which CSG delivery geography could plausibly fill a given req.

Deterministic and free -- no LLM call. CSG delivers from three regions with
different constraints, so the recommendation follows the role's own
requirements rather than keyword presence:

  US        onshore: client-facing, leadership, and compliance-sensitive work
            that cannot leave the country
  LATAM     nearshore: engineering/product work needing US timezone overlap
  Pakistan  offshore: async-tolerant technical work (data, QA, support,
            back-office)

`classify` returns the single best fit. A req that is explicitly onshore-only
(security clearance, "must be US citizen", onsite) can never be offshored, so
those markers are checked first and win outright.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

DeliveryFit = Literal["US", "LATAM", "Pakistan"]


def _compile(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.I) for p in patterns]


# Hard onshore requirements -- legal/compliance constraints that no offshore or
# nearshore model can satisfy. Checked first; these win outright.
_ONSHORE_REQUIRED = _compile([
    r"\bsecurity clearance\b", r"\btop secret\b", r"\bts/sci\b", r"\bpolygraph\b",
    r"\bus citizen", r"\bu\.s\. citizen", r"\bcitizenship required\b",
    r"\bgreen card\b", r"\bitar\b", r"\bfedramp\b", r"\bcjis\b",
    r"\bpublic trust\b", r"\bmust reside in the (?:us|united states)\b",
    r"\bonsite only\b", r"\bon-site only\b", r"\bno remote\b",
    r"\bhybrid\b.{0,40}\b(?:days? (?:per|a) week|in[- ]office)\b",
])

# Client-facing / leadership / compliance roles -- CSG staffs these onshore
# because they carry US stakeholder contact or accountability.
_ONSHORE_ROLE = _compile([
    r"\b(?:chief|vp|vice president|head of|director)\b",
    r"\bprincipal\b", r"\bstaff engineer\b",
    r"\b(?:account|customer success|client|partner|sales|solutions) (?:manager|executive|engineer|architect|director)\b",
    r"\bcustomer[- ]facing\b", r"\bstakeholder\b",
    r"\bcompliance\b", r"\bgrc\b", r"\baudit\b", r"\brisk\b",
    r"\bhipaa\b", r"\bsox\b", r"\bpci\b",
])

# Explicit region signals in the posting itself.
_LATAM_MARKERS = _compile([
    r"\blatam\b", r"\blatin america\b", r"\bnearshore\b",
    r"\bmexico\b", r"\bbrazil\b", r"\bargentina\b", r"\bcolombia\b",
    r"\bcosta rica\b", r"\bchile\b", r"\bperu\b", r"\buruguay\b",
    r"\b(?:americas|us)\s*time\s*zones?\b", r"\boverlap\b.{0,30}\b(?:pst|est|ct|us)\b",
])
_OFFSHORE_MARKERS = _compile([
    r"\bpakistan\b", r"\bkarachi\b", r"\blahore\b", r"\bislamabad\b",
    r"\boffshore\b", r"\basync\b", r"\basynchronous\b",
    r"\bfollow[- ]the[- ]sun\b", r"\banywhere in the world\b",
    r"\bfully (?:remote|distributed)\b.{0,40}\bglobal\b",
])

# Async-tolerant execution work -- suits offshore when nothing forces onshore.
_OFFSHORE_ROLE = _compile([
    r"\bqa\b", r"\bquality assurance\b", r"\bsdet\b", r"\btest engineer\b",
    r"\bmanual test", r"\bautomation test",
    r"\bdata (?:entry|annotation|labell?ing|processing)\b",
    r"\bback[- ]office\b", r"\bsupport engineer\b", r"\btier [123] support\b",
    r"\bhelp ?desk\b", r"\bdesktop support\b",
    r"\betl\b", r"\bdata pipeline\b",
])

# Core engineering/product -- the nearshore sweet spot when a US timezone
# overlap matters but the work is not client-facing.
_NEARSHORE_ROLE = _compile([
    r"\bsoftware engineer\b", r"\bdeveloper\b", r"\bfull[- ]?stack\b",
    r"\bback[- ]?end\b", r"\bfront[- ]?end\b", r"\bmobile\b",
    r"\bproduct manager\b", r"\bdesigner\b", r"\bdevops\b", r"\bsre\b",
    r"\bplatform engineer\b", r"\bdata engineer\b", r"\bml engineer\b",
])


def _any(patterns: list[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def classify(title: str, description: str = "", location: str = "") -> Optional[DeliveryFit]:
    """Best-fit delivery geography for one req, or None when nothing indicates.

    None is a real answer -- it means "no signal either way", and the caller
    should treat it as unknown rather than defaulting to a region.
    """
    title = title or ""
    blob = f"{title}\n{description or ''}\n{location or ''}"

    # 1. Hard constraints: legally cannot leave the US.
    if _any(_ONSHORE_REQUIRED, blob):
        return "US"

    # 2. Explicit region named in the posting.
    if _any(_OFFSHORE_MARKERS, blob):
        return "Pakistan"
    if _any(_LATAM_MARKERS, blob):
        return "LATAM"

    # 3. Role shape: seniority and client contact keep work onshore.
    if _any(_ONSHORE_ROLE, title):
        return "US"

    # 4. Async-tolerant execution work offshores well.
    if _any(_OFFSHORE_ROLE, title):
        return "Pakistan"

    # 5. Core engineering/product defaults to nearshore.
    if _any(_NEARSHORE_ROLE, title):
        return "LATAM"

    return None
