"""Config loading for the Outreach Agent.

Same shape as the Research Agent's csg/config.py: outreach.yaml is the single
source of truth for selection rules, the Research Agent's API location, and
sending/personalization settings. Editing it changes behavior with no code
changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
import os

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "outreach.yaml"

load_dotenv(PROJECT_ROOT / ".env")


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path

    @property
    def research(self) -> dict[str, Any]:
        return self.raw["research"]

    @property
    def research_api_base(self) -> str:
        # Env var wins over the committed config, so a dev/prod split never
        # needs an outreach.yaml edit.
        return os.environ.get("RESEARCH_API_BASE") or self.research["api_base"]

    @property
    def qualified_tiers(self) -> list[str]:
        return list(self.research.get("qualified_tiers") or ["A", "B"])

    @property
    def selection(self) -> dict[str, Any]:
        return self.raw["selection"]

    @property
    def sending(self) -> dict[str, Any]:
        return self.raw["sending"]

    @property
    def from_email(self) -> str:
        return os.environ.get("OUTREACH_FROM_EMAIL") or self.sending.get("from_email") or ""

    @property
    def from_name(self) -> str:
        return os.environ.get("OUTREACH_FROM_NAME") or self.sending.get("from_name") or "Outreach"

    @property
    def dry_run(self) -> bool:
        return bool(self.sending.get("dry_run", False))

    @property
    def personalization(self) -> dict[str, Any]:
        return self.raw["personalization"]

    @property
    def calendly_url(self) -> str:
        """The scheduling link the Conversation Agent's reply prompt offers
        an interested prospect. Env var wins over committed config, same
        convention as from_email, so a real link never has to be committed
        to the repo. Blank means "none configured" -- the reply prompt asks
        for a few times that work instead of linking anywhere."""
        return (os.environ.get("CALENDLY_URL") or self.personalization.get("calendly_url") or "").strip()

    @property
    def stages(self) -> dict[str, Any]:
        return self.raw["stages"]

    @property
    def stage_order(self) -> list[str]:
        return list(self.stages.get("order") or [
            "needs_review", "pending", "sent", "waiting", "replied", "booked", "not_interested",
        ])

    @property
    def conversation(self) -> dict[str, Any]:
        """Conversation Agent defaults for a campaign that has never been
        configured. The live per-campaign switch lives in the database
        (conversation_settings) -- this only seeds a new campaign's row, and
        an absent section still reads as fully disabled."""
        return self.raw.get("conversation") or {}

    @property
    def conversation_defaults(self) -> dict[str, Any]:
        cfg = self.conversation
        return {
            # Both switches default off: deploying the agent must never turn
            # itself on for an existing campaign, and switching it on must
            # never by itself authorize sending.
            "enabled": bool(cfg.get("enabled", False)),
            "auto_reply": bool(cfg.get("auto_reply", False)),
            "followup_after_days": int(cfg.get("followup_after_days", 3)),
            "max_followups": int(cfg.get("max_followups", 2)),
        }

    @property
    def imap_lookback_days(self) -> int:
        return int(self.conversation.get("imap_lookback_days", 14))


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    return Config(raw=raw, path=p)
