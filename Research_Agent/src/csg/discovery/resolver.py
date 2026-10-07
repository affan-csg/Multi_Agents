from typing import List

from ..dedupe import normalize_domain
from .base import CompanyCandidate


def deduplicate_and_resolve(candidates: List[CompanyCandidate]) -> List[CompanyCandidate]:
    """
    Deduplicates candidates by normalized domain (scheme/www/port/path/case
    insensitive -- see csg.dedupe.normalize_domain). Candidates with no
    usable domain are dropped.
    """
    seen = set()
    resolved = []
    for c in candidates:
        domain = normalize_domain(c.domain)
        if domain and domain not in seen:
            seen.add(domain)
            c.domain = domain
            resolved.append(c)
    return resolved
