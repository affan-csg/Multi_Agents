from typing import List
from .base import CompanyCandidate

def deduplicate_and_resolve(candidates: List[CompanyCandidate]) -> List[CompanyCandidate]:
    """
    Deduplicates candidates by domain.
    """
    seen = set()
    resolved = []
    for c in candidates:
        domain = c.domain.lower().strip()
        if domain.startswith("www."):
            domain = domain[4:]
            
        if domain not in seen:
            seen.add(domain)
            c.domain = domain
            resolved.append(c)
    return resolved
