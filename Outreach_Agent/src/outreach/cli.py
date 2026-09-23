"""CLI for the Outreach Agent.

`import-prospects` pulls qualified companies from the Research Agent's API
into Outreach's local cache. `campaign create` selects the configured top-N
of those into a new campaign. `campaign send` drafts + sends email for every
pending item in a campaign. `pipeline` (Overview table) reads what's already
been sent -- no API calls.

The Outreach Agent's own job stops at Sent. Everything after that belongs to
the Conversation Agent (`conversation` commands below, see
src/outreach/conversation/), which is off by default per campaign -- until it
is switched on, waiting/replied/booked/not_interested are set by hand via
`item set-stage` or the Dashboard board, exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import db, ingest, pipeline
from .config import load_config

app = typer.Typer(add_completion=False, help="CSG Outreach Agent")
campaign_app = typer.Typer(add_completion=False, help="Manage outreach campaigns")
item_app = typer.Typer(add_completion=False, help="Manage individual outreach items")
conversation_app = typer.Typer(add_completion=False, help="Conversation Agent (replies, follow-ups)")
app.add_typer(campaign_app, name="campaign")
app.add_typer(item_app, name="item")
app.add_typer(conversation_app, name="conversation")

console = Console()


def _cfg(path: Optional[Path]):
    try:
        return load_config(path)
    except Exception as exc:
        console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(1)


@app.command("import-prospects")
def import_prospects_cmd(
    run_id: str = typer.Argument(..., help="Research Agent run_id (or 'all')"),
    tiers: str = typer.Option("A,B", "--tiers", help="Comma-separated tiers to import"),
    min_score: Optional[float] = typer.Option(None, "--min-score"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n"),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Pull qualified prospects from the Research Agent's API into the local cache."""
    cfg = _cfg(config)
    conn = db.connect()
    try:
        imported, excluded = ingest.import_prospects(
            conn, cfg, run_id,
            tiers=[t.strip().upper() for t in tiers.split(",")],
            min_score=min_score, limit=limit,
        )
        console.print(f"[green]imported[/green] {len(imported)} prospect(s) from run {run_id}")
        for p in imported:
            console.print(f"  {p.tier or '?':<2} {p.score or 0:>5.1f}  {p.research_domain:<30} {p.contact_email or '(no contact)'}")
        for e in excluded:
            console.print(f"[yellow]no contacts found[/yellow] for {e['name']} ({e['domain']}, Tier {e['tier']}) "
                          "-- not added to prospects")
    finally:
        conn.close()


@campaign_app.command("create")
def campaign_create(
    name: Optional[str] = typer.Option(None, "--name"),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Restrict to prospects imported from this run"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n"),
    tiers: Optional[str] = typer.Option(None, "--tiers"),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
) -> None:
    """Select the configured top-N cached prospects into a new campaign."""
    cfg = _cfg(config)
    conn = db.connect()
    try:
        campaign, excluded = ingest.create_campaign_from_prospects(
            conn, cfg, name=name, research_run_id=run_id, limit=limit,
            tiers=[t.strip().upper() for t in tiers.split(",")] if tiers else None,
        )
        items = db.list_items(conn, campaign_id=campaign.campaign_id)
        console.print(f"[green]created campaign[/green] {campaign.campaign_id}  \"{campaign.name}\"  "
                      f"({len(items)} prospect(s) selected)")
        for e in excluded:
            console.print(f"[yellow]no contacts found[/yellow] for {e['name']} ({e['domain']}, Tier {e['tier']}) "
                          "-- not added to campaign")
    finally:
        conn.close()


@campaign_app.command("send")
def campaign_send(
    campaign_id: str = typer.Argument(...),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Draft + send email for every pending item in a campaign."""
    cfg = _cfg(config)
    conn = db.connect()
    try:
        pending = [i for i in db.list_items(conn, campaign_id=campaign_id) if i.stage == "pending"]
        if not pending:
            console.print("[yellow]nothing pending to send[/yellow]")
            return
        if cfg.dry_run:
            console.print("[dim]dry_run is enabled in outreach.yaml -- no real email will be sent[/dim]")
        if not yes and not typer.confirm(f"Send {len(pending)} email(s) for campaign {campaign_id}?"):
            raise typer.Abort()

        with console.status("[cyan]sending...") as status:
            def progress(done: int, total: int, domain: str) -> None:
                status.update(f"[cyan]sending[/cyan] {done}/{total}  {domain}")

            stats = pipeline.send_campaign(conn, cfg, campaign_id, on_progress=progress)

        console.print(f"[green]sent[/green] {stats.sent}   [red]failed[/red] {stats.failed}   "
                      f"[yellow]skipped[/yellow] {stats.skipped}")
        for domain, err in stats.errors.items():
            console.print(f"  [red]{domain}[/red]: {err}")
    finally:
        conn.close()


@campaign_app.command("list")
def campaign_list() -> None:
    conn = db.connect()
    try:
        campaigns = db.list_campaigns(conn)
        t = Table()
        for col in ("campaign_id", "name", "channel", "created_at"):
            t.add_column(col)
        for c in campaigns:
            t.add_row(c.campaign_id, c.name or "", c.channel, str(c.created_at or ""))
        console.print(t)
    finally:
        conn.close()


@campaign_app.command("delete")
def campaign_delete(
    campaign_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a campaign and its outreach items/events. Cached prospects are untouched."""
    conn = db.connect()
    try:
        campaign = db.get_campaign(conn, campaign_id)
        if not campaign:
            console.print(f"[red]{campaign_id} not found[/red]")
            raise typer.Exit(1)
        if not yes and not typer.confirm(f"Delete campaign \"{campaign.name or campaign_id}\" and all its items?"):
            raise typer.Abort()
        db.delete_campaign(conn, campaign_id)
        console.print(f"[green]deleted[/green] {campaign_id}")
    finally:
        conn.close()


@campaign_app.command("pipeline")
def campaign_pipeline(campaign_id: str = typer.Argument(...)) -> None:
    """The 5-stage pipeline board for one campaign, as a table."""
    conn = db.connect()
    try:
        items = db.list_items(conn, campaign_id=campaign_id)
        t = Table(title=f"Pipeline -- {campaign_id}")
        for col in ("stage", "domain", "to_email", "sent_at"):
            t.add_column(col)
        for i in sorted(items, key=lambda i: i.stage):
            t.add_row(i.stage, i.research_domain, i.to_email or "", str(i.sent_at or ""))
        console.print(t)
    finally:
        conn.close()


@item_app.command("set-stage")
def item_set_stage(
    item_id: str = typer.Argument(...),
    stage: str = typer.Argument(..., help="waiting|replied|booked|not_interested (pending/sent/failed are agent-owned)"),
) -> None:
    """Manually advance (or re-open) an item past 'sent', as a person.

    Always available, whether or not the Conversation Agent is switched on
    for the campaign -- a person's move always overrides the agent's."""
    if stage not in ("waiting", "replied", "booked", "not_interested"):
        console.print("[red]only waiting/replied/booked/not_interested can be set manually here[/red]")
        raise typer.Exit(1)
    conn = db.connect()
    try:
        db.set_stage(conn, item_id, stage, actor="user")
        console.print(f"[green]{item_id}[/green] -> {stage}")
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Conversation Agent -- see src/outreach/conversation/
# ----------------------------------------------------------------------------

@conversation_app.command("status")
def conversation_status(campaign_id: str = typer.Argument(...)) -> None:
    """Show whether the Conversation Agent is on for a campaign, and how it
    is configured."""
    from .conversation.settings import get_settings

    conn = db.connect()
    try:
        s = get_settings(conn, campaign_id)
        t = Table(title=f"Conversation Agent -- {campaign_id}")
        t.add_column("setting")
        t.add_column("value")
        t.add_row("enabled", "[green]ON[/green]" if s.enabled else "[dim]OFF[/dim]")
        t.add_row("auto_reply", "[yellow]ON[/yellow]" if s.auto_reply else "[dim]OFF (drafts only)[/dim]")
        t.add_row("followup_after_days", str(s.followup_after_days))
        t.add_row("max_followups", str(s.max_followups))
        console.print(t)
        if s.enabled and not s.auto_reply:
            console.print("[dim]Replies are drafted for approval, never sent automatically.[/dim]")
    finally:
        conn.close()


@conversation_app.command("enable")
def conversation_enable(
    campaign_id: str = typer.Argument(...),
    auto_reply: bool = typer.Option(
        False, "--auto-reply",
        help="Also let the agent SEND replies itself. Off by default: it drafts them for approval.",
    ),
) -> None:
    """Switch the Conversation Agent on for a campaign.

    On its own this never sends anything -- the agent detects replies,
    classifies them, moves cards and writes drafts. Pass --auto-reply to also
    let it send those drafts unattended."""
    from .conversation.settings import set_settings

    conn = db.connect()
    try:
        s = set_settings(conn, campaign_id, enabled=True, auto_reply=auto_reply)
        console.print(f"[green]conversation agent ON[/green] for {campaign_id}")
        if s.auto_reply:
            console.print("[yellow]auto_reply is ON -- replies will be sent without review.[/yellow]")
        else:
            console.print("[dim]auto_reply is OFF -- replies are drafted for your approval.[/dim]")
    finally:
        conn.close()


@conversation_app.command("disable")
def conversation_disable(campaign_id: str = typer.Argument(...)) -> None:
    """Switch the Conversation Agent off for a campaign.

    Stops all automated processing immediately. Nothing is deleted or reset:
    every message, stage and counter stays exactly as it is, and cards can be
    moved by hand from here on. Re-enabling resumes without replaying
    anything."""
    from .conversation.settings import set_settings

    conn = db.connect()
    try:
        set_settings(conn, campaign_id, enabled=False)
        console.print(f"[dim]conversation agent OFF[/dim] for {campaign_id}")
        console.print("[dim]Existing data untouched; move cards manually with `item set-stage`.[/dim]")
    finally:
        conn.close()


@conversation_app.command("tick")
def conversation_tick(
    campaign_id: Optional[str] = typer.Argument(
        None, help="Campaign to check. Omit to tick every enabled campaign.",
    ),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Run one conversation cycle now: fetch replies, classify them, advance
    cards, and handle follow-ups.

    A no-op for any campaign with the agent switched off."""
    from .conversation import engine

    cfg = _cfg(config)
    conn = db.connect()
    try:
        results = (
            [engine.tick(conn, cfg, campaign_id)] if campaign_id
            else engine.tick_all(conn, cfg)
        )
        if not results:
            console.print("[dim]no campaigns have the conversation agent enabled[/dim]")
            return

        for r in results:
            console.print(f"\n[bold]{r.campaign_id}[/bold]")
            if not r.enabled:
                console.print(f"  [dim]{r.message}[/dim]")
                continue
            if r.message:
                console.print(f"  [yellow]{r.message}[/yellow]")
            console.print(
                f"  fetched={r.fetched} matched={r.matched} new={r.new_inbound} "
                f"duplicates={r.duplicates} unmatched={r.unmatched}"
            )
            console.print(
                f"  advanced={r.advanced} to_waiting={r.moved_to_waiting} "
                f"replies_sent={r.replies_sent} replies_drafted={r.replies_drafted} "
                f"followups_sent={r.followups_sent} followups_drafted={r.followups_drafted}"
            )
            if r.classified:
                console.print(f"  classified: {r.classified}")
            for domain, err in r.errors.items():
                console.print(f"  [red]{domain}[/red]: {err}")
    finally:
        conn.close()


if __name__ == "__main__":
    app()
