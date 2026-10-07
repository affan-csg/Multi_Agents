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


@dataclass(frozen=True)
class SenderAccount:
    """One sending mailbox. `from_email` is the identity analytics group by --
    it is stamped on every item/message the account sends, so it must be
    unique per account (compared case-insensitively)."""

    from_email: str
    from_name: str
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    is_default: bool = False

    @property
    def identity(self) -> str:
        """What gets stored as the sender: the From address, or the SMTP login
        when no From address is configured (the From header then defaults to
        the login on most relays)."""
        return self.from_email or self.smtp_username


def _account_env(prefix: str, name: str, fallback: str = "") -> str:
    return (os.environ.get(f"{prefix}_{name}") or fallback or "").strip()


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path

    @property
    def research(self) -> dict[str, Any]:
        return self.raw["research"]

    @property
    def research_api_base(self) -> str:
        service_url = os.environ.get("RESEARCH_SERVICE_URL")
        if service_url:
            return f"{service_url.rstrip('/')}/research"
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
    def accounts(self) -> list[SenderAccount]:
        """Every sending account, default first.

        The default account is the pre-existing single-account setup
        (OUTREACH_FROM_EMAIL / SMTP_* / IMAP_* env vars), unchanged. Further
        accounts are listed in outreach.yaml under `sending.accounts`; each
        entry has `from_email`, optional `from_name`, and an `env_prefix` whose
        `<PREFIX>_SMTP_USERNAME`, `_SMTP_PASSWORD`, `_SMTP_HOST`, `_SMTP_PORT`,
        `_IMAP_USERNAME`, `_IMAP_PASSWORD`, `_IMAP_HOST`, `_IMAP_PORT` env vars
        hold its secrets (host/port fall back to the default account's, so two
        mailboxes on one provider need only a username and password)."""
        default = SenderAccount(
            from_email=self.from_email,
            from_name=self.from_name,
            smtp_host=(os.environ.get("SMTP_HOST") or "").strip(),
            smtp_port=int(os.environ.get("SMTP_PORT") or 587),
            smtp_username=(os.environ.get("SMTP_USERNAME") or "").strip(),
            smtp_password=os.environ.get("SMTP_PASSWORD") or "",
            imap_host=(os.environ.get("IMAP_HOST") or "").strip(),
            imap_port=int(os.environ.get("IMAP_PORT") or 993),
            imap_username=(os.environ.get("IMAP_USERNAME") or os.environ.get("SMTP_USERNAME") or "").strip(),
            imap_password=os.environ.get("IMAP_PASSWORD") or os.environ.get("SMTP_PASSWORD") or "",
            is_default=True,
        )
        out = [default]
        seen = {default.from_email.lower()}
        for entry in self.sending.get("accounts") or []:
            email_addr = str(entry.get("from_email") or "").strip()
            prefix = str(entry.get("env_prefix") or "").strip()
            if not email_addr or not prefix or email_addr.lower() in seen:
                continue
            seen.add(email_addr.lower())
            smtp_user = _account_env(prefix, "SMTP_USERNAME")
            smtp_pass = os.environ.get(f"{prefix}_SMTP_PASSWORD") or ""
            out.append(SenderAccount(
                from_email=email_addr,
                from_name=str(entry.get("from_name") or default.from_name),
                smtp_host=_account_env(prefix, "SMTP_HOST", default.smtp_host),
                smtp_port=int(_account_env(prefix, "SMTP_PORT") or default.smtp_port),
                smtp_username=smtp_user,
                smtp_password=smtp_pass,
                imap_host=_account_env(prefix, "IMAP_HOST", default.imap_host),
                imap_port=int(_account_env(prefix, "IMAP_PORT") or default.imap_port),
                imap_username=_account_env(prefix, "IMAP_USERNAME", smtp_user),
                imap_password=os.environ.get(f"{prefix}_IMAP_PASSWORD") or smtp_pass,
            ))
        return out

    def account_for(self, sender_email: str | None = None) -> SenderAccount:
        """The account that sends as `sender_email`, or the default account
        when none is given or it is no longer configured. Follow-ups and
        replies pass the item's stored sender so a conversation always
        continues from the mailbox that started it."""
        accounts = self.accounts
        wanted = (sender_email or "").strip().lower()
        if wanted:
            for acct in accounts:
                if acct.from_email.lower() == wanted:
                    return acct
        return accounts[0]

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
    def calendly_api_token(self) -> str:
        """Personal access token for Calendly's REST API (scheduled_events,
        webhook subscriptions) -- distinct from calendly_url above, which is
        just the static scheduling link offered in reply text. Env-only, no
        YAML fallback: a real API token has no sensible value to commit to
        outreach.yaml. Blank means the sync/webhook features are inert -- see
        calendly/client.py."""
        return (os.environ.get("CALENDLY_API_TOKEN") or "").strip()

    @property
    def calendly_webhook_signing_key(self) -> str:
        """The signing key Calendly returns when a webhook subscription is
        created (POST /webhook_subscriptions' response `signing_key`), used
        to verify the Calendly-Webhook-Signature header on every inbound
        webhook call. Blank means webhook verification cannot succeed -- see
        calendly/webhook.py."""
        return (os.environ.get("CALENDLY_WEBHOOK_SIGNING_KEY") or "").strip()

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
            "followup_after_days": int(cfg.get("followup_after_days", 2)),
            "max_followups": int(cfg.get("max_followups", 2)),
            "followup_days": [int(d) for d in cfg.get("followup_days", [7, 14])],
            "no_response_after_days": int(cfg.get("no_response_after_days", 21)),
        }

    @property
    def imap_lookback_days(self) -> int:
        return int(self.conversation.get("imap_lookback_days", 14))


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    return Config(raw=raw, path=p)
