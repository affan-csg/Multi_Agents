from abc import ABC, abstractmethod
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel

class CompanyCandidate(BaseModel):
    domain: str
    name: Optional[str] = None
    source: str
    source_url: Optional[str] = None
    location: Optional[str] = None
    industry: Optional[str] = None
    employee_count: Optional[int] = None
    discovered_at: datetime

class DiscoveryProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    async def discover(self, region: str, limit: int = 10) -> List[CompanyCandidate]:
        """Discover companies matching criteria."""
        pass
