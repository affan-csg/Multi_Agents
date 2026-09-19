"""CLI.

`discover` is the entrypoint: give it a region and it finds, researches,
scores, and ranks companies against the ICP in config/icp.yaml. It needs
GEMINI_API_KEY (discovery and firmographic research are LLM-backed); the
scoring that follows is deterministic and free.

`report`/`show`/`export`/`suppress` read what a run produced -- no API calls.
`qualify` needs ANTHROPIC_API_KEY or GEMINI_API_KEY (see qualify.py), and
`contacts` spends Hunter.io quota. Both are explicit, never automatic.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import (contacts as contacts_mod, db, discover as discover_mod, export,
               pipeline, qualify as qualify_mod)
from .config import load_config
from .discovery import DiscoveryError
from .models import provenance_value

app = typer.Typer(add_completion=False, help="CSG Research/ICP Agent")
console = Console()


def _cfg(path: Optional[Path]):
    try:
        return load_config(path)
    except Exception as exc:
        console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(1)


def _funnel(s: pipeline.Stats) -> None:
    t = Table(title="Funnel", show_header=False, box=None, pad_edge=False)
    t.add_column(style="dim", width=26)
    t.add_column(justify="right")
    t.add_row("companies", str(s.companies))
    t.add_row("suppressed", str(s.suppressed))
    t.add_row("no ATS board found", str(s.no_ats))
    t.add_row("ATS resolved", str(s.ats_found))
    t.add_row("  board empty (0 reqs)", str(s.no_reqs))
    t.add_row("reqs fetched", str(s.reqs_fetched))
    t.add_row("disqualified", str(s.disqualified))
    t.add_row("[bold]scored[/bold]", f"[bold]{s.scored}[/bold]")
    console.print(t)

    if s.ats_found:
        console.print(f"\n[dim]detection:[/dim] " + "  ".join(f"{k}={v}" for k, v in s.detected_by.items())
                      + "   [dim]providers:[/dim] " + "  ".join(f"{k}={v}" for k, v in s.providers.items()))

    tt = Table(title="\nTiers", show_header=True, box=None)
    for tier in ("A", "B", "C", "D"):
        tt.add_column(tier, justify="center")
    tt.add_row(*(f"[bold]{s.tiers[x]}[/bold]" for x in ("A", "B", "C", "D")))
    console.print(tt)

    if s.dq_reasons:
        console.print("\n[dim]disqualification reasons:[/dim]")
        for reason, n in sorted(s.dq_reasons.items(), key=lambda kv: -kv[1]):
            console.print(f"  {n:>4}  {reason}")


@app.command()
def discover(
    region: Optional[str] = typer.Argument(
        None, help="Region to search (e.g. 'GA', 'East Coast'). "
                   "Defaults to the active region in icp.yaml."),
    limit: int = typer.Option(100, "--limit", "-n", help="Max companies to discover"),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass the HTTP cache"),
    label: Optional[str] = typer.Option(None, "--label", help="Human name for this run"),
    auto_qualify_top: Optional[int] = typer.Option(
        None, "--auto-qualify-top", help="After discovery, take the top N scored companies "
        "(by score) and qualify + find contacts for whichever of those land in Tier A or B "
        "-- a lower-tier company in the top N is found and shown, but not qualified/contacted. "
        "Costs real Gemini tokens and Hunter.io quota -- shows one combined confirmation before "
        "spending (skip with --yes)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the --auto-qualify-top cost confirmation"),
) -> None:
    """Autonomous pipeline: discover -> enrich -> ATS -> score -> tier -> export.

    The region is the only input; every other criterion (size, financials,
    practice areas, priority) comes from the embedded ICP in icp.yaml.

    Contact discovery and qualification are separate steps by default -- run
    `csg qualify`/`csg contacts` afterwards, or pass --auto-qualify-top N to
    find the top N and chain qualify+contacts for whichever of those are
    Tier A/B, with one confirmation.
    """
    cfg = _cfg(config)
    target = cfg.resolve_region(region)

    console.print(f"\n[bold cyan]{'='*60}[/bold cyan]")
    console.print(f"[bold]CSG Autonomous Discovery[/bold] -- "
                  f"Region: [green]{target}[/green]  Limit: {limit}")
    console.print(f"[dim]profile={cfg.raw['profile']}  "
                  f"ICP: {int(cfg.size['sweet_min'])}-{int(cfg.size['sweet_max'])} emp, "
                  f"{len(cfg.enabled_areas)} practice areas[/dim]")
    console.print(f"[bold cyan]{'='*60}[/bold cyan]\n")

    conn = db.connect()
    try:
        # The qualify+contacts add-on spends real quota, so ask before
        # discover_mod.run_discovery does any of that work -- confirming
        # after the fact would mean the spend already happened. Preview the
        # same top-N/tier-A/B selection run_discovery will use.
        confirmed_top = auto_qualify_top
        if auto_qualify_top and not yes:
            confirmed_top = _confirm_auto_qualify(auto_qualify_top)
            if confirmed_top is None:
                return

        with console.status("[cyan]starting...") as status:
            def progress(stage: str, done: int, total: int, detail: str) -> None:
                status.update(f"[cyan]{stage}[/cyan] {done}/{total}  {detail}")

            try:
                stats = asyncio.run(discover_mod.run_discovery(
                    cfg, region=region, limit=limit, conn=conn,
                    label=label, use_cache=not no_cache, on_progress=progress,
                    auto_qualify_top=confirmed_top,
                    # The CLI keeps its explicit --auto-qualify-top opt-in +
                    # cost confirmation above rather than the API route's
                    # automatic-by-default behavior -- a person running raw
                    # commands should consciously choose to spend quota.
                    auto_pipeline=False,
                ))
            except DiscoveryError as exc:
                console.print(f"[red]discovery failed:[/red] {exc}")
                raise typer.Exit(1)

        if not stats.discovered:
            console.print("[yellow]No companies discovered.[/yellow] "
                          "Try a different region or raise --limit.")
            return

        console.print(f"[green]discovered[/green] {stats.discovered}   "
                      f"[green]enriched[/green] {stats.enriched}   "
                      f"[dim]reused {stats.enrich_skipped} already researched[/dim]\n")

        if stats.pipeline:
            _funnel(stats.pipeline)

        for kind, path in stats.exports.items():
            console.print(f"[green]exported[/green] {path}")

        _top_prospects(conn, stats.run_id, target)

        if confirmed_top:
            _print_auto_qualify_result(stats)
    finally:
        conn.close()


def _confirm_auto_qualify(top_n: int) -> Optional[int]:
    """Cost confirmation for --auto-qualify-top, asked before any run starts
    since discover_mod.run_discovery spends the quota itself now. Returns
    top_n to proceed, or None if the user declined."""
    try:
        provider = qualify_mod.backend_name()
    except qualify_mod.QualificationError as exc:
        console.print(f"\n[red]--auto-qualify-top skipped:[/red] {exc}")
        return None
    console.print(f"\n[bold]--auto-qualify-top {top_n}[/bold]  "
                  f"qualify provider={provider}  contacts=hunter.io")
    proceed = typer.confirm(
        f"After discovery, this will qualify + look up Hunter.io contacts for up to {top_n} "
        f"top-scored Tier A/B companies (LLM calls against {provider}; free tier: 25 Hunter.io "
        "searches/MONTH total). Continue?", default=False,
    )
    if not proceed:
        console.print("[yellow]--auto-qualify-top aborted[/yellow] (discovery/scoring will still run)")
        return None
    return top_n


def _print_auto_qualify_result(stats: discover_mod.DiscoveryStats) -> None:
    if stats.auto_qualify_skipped_tier:
        console.print(f"\n[dim]--auto-qualify-top: {stats.auto_qualify_skipped_tier}/"
                      f"{stats.auto_qualify_top_n} of the top {stats.auto_qualify_top_n} are "
                      f"below Tier A/B -- skipped qualify/contacts for those.[/dim]")
    if stats.qualify is None and stats.contacts is None:
        console.print(f"\n[yellow]--auto-qualify-top: no Tier A/B companies in the top "
                      f"{stats.auto_qualify_top_n} to qualify.[/yellow]")
        return
    if stats.qualify and "error" in stats.qualify:
        console.print(f"\n[red]qualify failed:[/red] {stats.qualify['error']}")
    elif stats.qualify:
        console.print(f"\n[green]qualified[/green] {stats.qualify['qualified']}   "
                      f"[yellow]errors[/yellow] {stats.qualify['errors']}")
    if stats.contacts and "error" in stats.contacts:
        console.print(f"[red]contacts failed:[/red] {stats.contacts['error']}")
    elif stats.contacts:
        console.print(f"[green]found[/green] {stats.contacts['found']} companies   "
                      f"[green]{stats.contacts['contacts_saved']}[/green] contacts saved   "
                      f"[yellow]errors[/yellow] {stats.contacts['errors']}")
    console.print(f"\nRe-export to include LLM + contact fields: [bold]csg export --run {stats.run_id}[/bold]")

    if stats.outreach and "error" in stats.outreach:
        console.print(f"[red]Outreach hand-off failed:[/red] {stats.outreach['error']}")
    elif stats.outreach:
        console.print(f"[green]Outreach:[/green] {stats.outreach['imported']} prospect(s) imported, "
                      f"{stats.outreach['item_count']} campaign item(s) drafted "
                      f"(campaign {stats.outreach.get('campaign_id') or '-'})")


def _reqs_cell(r: dict) -> str:
    """Render a score row's open-req count for a CLI table, distinguishing a
    confirmed-empty board (plain "0") from a company whose job signal is
    unknown (job discovery exhausted -- "0?", never a bare "0" that reads as
    confirmed). See models.py's HiringSignal / scoring.py's module docstring
    -- this is the CLI's equivalent of the Dashboard's hiring-signal badge."""
    n = r.get("metrics", {}).get("req_count", "")
    if n in ("", None):
        return ""
    if n == 0 and r.get("hiring_signal") == "unknown":
        return "0?"
    return str(n)


def _top_prospects(conn, run_id: str, region: str, limit: int = 100) -> list[dict]:
    """Ranked prospect table -- the run's actual deliverable, printed for the
    operator. discover_mod.run_discovery does its own, identical ranking
    internally for --auto-qualify-top, so this is display-only."""
    rows = [r for r in db.get_scores(conn, run_id) if not r.get("disqualified")]
    ranked = sorted(rows, key=lambda r: -r["score"])[:limit]
    if not ranked:
        console.print("\n[yellow]No qualified prospects in this run.[/yellow]")
        return []

    console.print(f"\n[bold]TOP {len(ranked)} PROSPECTS -- {region}[/bold]")
    t = Table(title=f"run {run_id}")
    t.add_column("#", justify="right", style="dim")
    t.add_column("T", justify="center")
    t.add_column("Score", justify="right")
    t.add_column("Domain")
    t.add_column("Name")
    t.add_column("Emp", justify="right")
    t.add_column("Revenue", justify="right")
    t.add_column("Reqs", justify="right")

    colors = {"A": "bold green", "B": "cyan", "C": "yellow", "D": "dim"}
    for idx, r in enumerate(ranked, 1):
        t.add_row(
            str(idx),
            f"[{colors.get(r['tier'], 'dim')}]{r['tier']}[/]",
            f"{r['score']:.0f}",
            r["domain"],
            (r.get("name") or "")[:28],
            str(provenance_value(r.get("employee_count")) or "-"),
            str(provenance_value(r.get("revenue")) or "-"),
            _reqs_cell(r),
        )
    console.print(t)
    console.print(f"\n[bold green]Discovery complete.[/bold green]  "
                  f"Contacts: [bold]csg contacts --run {run_id}[/bold]   "
                  f"Inspect: [bold]csg show <domain>[/bold]\n"
                  f"[dim]Reqs \"0?\" = job signal unknown (no board/careers page found), "
                  f"not a confirmed zero.[/dim]\n")
    return ranked


@app.command()
def report(
    run_id: Optional[str] = typer.Option(None, "--run", "-r", help="Defaults to latest run"),
    tier: Optional[str] = typer.Option(None, "--tier", "-t", help="Filter, e.g. A or AB"),
    limit: int = typer.Option(30, "--limit", "-n"),
) -> None:
    """Ranked prospect table for a run."""
    conn = db.connect()
    rid = run_id or db.latest_run_id(conn)
    if not rid:
        console.print("[yellow]no runs yet[/yellow]")
        raise typer.Exit(1)

    rows = db.get_scores(conn, rid)
    if tier:
        want = set(tier.upper())
        rows = [r for r in rows if r["tier"] in want]
    rows = [r for r in rows if r["tier"] != "D"][:limit] if not tier else rows[:limit]

    t = Table(title=f"Prospects -- run {rid}")
    t.add_column("T", justify="center")
    t.add_column("Score", justify="right")
    t.add_column("Domain")
    t.add_column("Reqs", justify="right")
    t.add_column("Rel", justify="right")
    t.add_column("MedAge", justify="right")
    t.add_column("Areas")
    t.add_column("HN/FP/FF/BS", justify="right")

    colors = {"A": "bold green", "B": "cyan", "C": "yellow", "D": "dim"}
    for r in rows:
        m, d = r["metrics"], r["dimensions"]
        med = m.get("median_age_days")
        t.add_row(
            f"[{colors[r['tier']]}]{r['tier']}[/]",
            f"{r['score']:.0f}",
            r["domain"],
            _reqs_cell(r),
            str(m.get("relevant_reqs", "")),
            f"{med:.0f}d" if isinstance(med, (int, float)) else "-",
            ", ".join(m.get("top_practice_areas") or [])[:34],
            "/".join(f"{d.get(k, 0):.0f}" for k in
                     ("Hiring Need", "Fill Pain", "Firmographic Fit", "Buying Signals")),
        )
    console.print(t)
    console.print(f"[dim]{len(rows)} shown. HN/FP/FF/BS = the four dimension scores. "
                  f"Reqs \"0?\" = job signal unknown, not a confirmed zero.[/dim]")
    conn.close()


@app.command()
def show(domain: str, run_id: Optional[str] = typer.Option(None, "--run", "-r")) -> None:
    """Full scoring breakdown for one company -- every point, explained."""
    conn = db.connect()
    rid = run_id or db.latest_run_id(conn)
    rows = [r for r in db.get_scores(conn, rid or "") if r["domain"] == domain]

    company = db.get_company(conn, domain)
    if not company:
        console.print(f"[yellow]{domain} not in database[/yellow]")
        raise typer.Exit(1)

    console.print(f"\n[bold]{company.name or domain}[/bold]  ({domain})")
    console.print(f"status={company.status}  ats={company.ats_provider}:{company.ats_token} "
                  f"via {company.ats_detected_by}")
    console.print(f"job discovery: method={company.job_discovery_method} "
                  f"source={company.job_source} hiring_signal={company.hiring_signal}")
    if company.hiring_signal == "unknown" and company.job_discovery_notes:
        console.print(f"[yellow]  {company.job_discovery_notes}[/yellow]")
    if company.dq_reason:
        console.print(f"[red]disqualified:[/red] {company.dq_reason}")

    if rows:
        r = rows[0]
        console.print(f"\n[bold]score {r['score']:.1f}  tier {r['tier']}[/bold]")
        for k, v in r["dimensions"].items():
            console.print(f"  {k:<20} {v:>6.1f}")
        console.print("\n[bold]why:[/bold]")
        for line in r["explanation"]:
            console.print(f"  {line}")
        console.print(f"\n[dim]completeness: {json.dumps(r['metrics'].get('data_completeness', {}))}[/dim]")

        qual = db.get_qualification(conn, domain, rid) if rid else None
        if qual:
            console.print(f"\n[bold]pain summary[/bold] [dim]({qual['model']})[/dim]")
            console.print(f"  {qual['pain_summary']}")
            console.print(f"\n[bold]recommended angle[/bold]")
            console.print(f"  {qual['recommended_angle']}")

    people = db.get_contacts(conn, domain)
    if people:
        ct = Table(title=f"\nContacts ({len(people)}) [dim]via Hunter.io[/dim]")
        ct.add_column("Name")
        ct.add_column("Position")
        ct.add_column("Email")
        ct.add_column("Conf.", justify="right")
        ct.add_column("Verified")
        for p in people:
            name = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or "-"
            ct.add_row(name, p.get("position") or "-", p["email"],
                       str(p.get("confidence") or "-"), p.get("verification") or "-")
        console.print(ct)

    postings = db.get_postings(conn, domain)
    if postings:
        cfg = load_config()
        now = db.utcnow()
        t = Table(title=f"\nOpen reqs ({len(postings)})")
        t.add_column("Title")
        t.add_column("Location")
        t.add_column("Age", justify="right")
        t.add_column("Practice area")
        for p in postings[:25]:
            age = p.age_days(now)
            t.add_row(p.title[:52], (p.location or "-")[:26],
                      f"{age:.0f}d" if age is not None else "-",
                      ", ".join(cfg.classify(p.title)) or "[dim]-[/dim]")
        console.print(t)
    conn.close()


@app.command()
def suppress(domain: str, reason: str = typer.Option("client", "--reason")) -> None:
    """Add a domain to the suppression list (existing clients, DNC, open deals)."""
    conn = db.connect()
    db.add_suppression(conn, domain.lower().strip(), reason)
    console.print(f"[green]suppressed[/green] {domain} ({reason})")
    conn.close()


@app.command("export")
def export_cmd(
    run_id: Optional[str] = typer.Option(None, "--run", "-r"),
    tier: Optional[str] = typer.Option(None, "--tier", "-t"),
) -> None:
    """Re-export a run to CSV + JSONL."""
    conn = db.connect()
    rid = run_id or db.latest_run_id(conn)
    tiers = list(tier.upper()) if tier else None
    console.print(f"[green]{export.to_csv(conn, rid, tiers=tiers)}[/green]")
    console.print(f"[green]{export.to_jsonl(conn, rid, tiers=tiers)}[/green]")
    conn.close()


@app.command()
def qualify(
    run_id: Optional[str] = typer.Option(None, "--run", "-r", help="Defaults to latest run"),
    tier: str = typer.Option("AB", "--tier", "-t", help="Tiers to qualify, e.g. A or AB"),
    limit_reqs: int = typer.Option(8, "--limit-reqs", help="Open reqs sent to the model per company"),
    force: bool = typer.Option(False, "--force", help="Re-qualify domains already qualified for this run"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the cost confirmation prompt"),
) -> None:
    """Step 4: LLM pass over scored prospects -- adds pain_summary + recommended_angle.

    Uses Gemini (GEMINI_API_KEY) if set, else Claude (ANTHROPIC_API_KEY). Costs
    real tokens/quota, so this asks for confirmation first (skip with --yes).
    Already-qualified domains are skipped unless --force is passed. Re-export
    afterward with `csg export` to include qualification fields in the JSONL
    handoff.
    """
    conn = db.connect()
    rid = run_id or db.latest_run_id(conn)
    if not rid:
        console.print("[yellow]no runs yet[/yellow]")
        raise typer.Exit(1)

    tiers = list(tier.upper())
    pending = qualify_mod.pending_rows(conn, rid, tiers, force=force)
    if not pending:
        console.print(f"[yellow]nothing to qualify[/yellow] for run {rid} tiers={''.join(tiers)} "
                       "(already qualified -- use --force to re-qualify)")
        conn.close()
        raise typer.Exit(0)

    try:
        provider = qualify_mod.backend_name()
    except qualify_mod.QualificationError as exc:
        console.print(f"[red]{exc}[/red]")
        conn.close()
        raise typer.Exit(1)

    console.print(f"[bold]qualifying run {rid}[/bold]  tiers={''.join(tiers)}  "
                  f"provider={provider}  companies={len(pending)}")
    if not yes:
        proceed = typer.confirm(
            f"This will make {len(pending)} LLM call(s) against {provider} "
            f"(costs real tokens/quota). Continue?", default=False,
        )
        if not proceed:
            console.print("[yellow]aborted[/yellow]")
            conn.close()
            raise typer.Exit(0)

    with console.status("[cyan]working...") as status:
        def progress(done: int, total: int, domain: str) -> None:
            status.update(f"[cyan]{done}/{total}[/cyan]  {domain}")

        try:
            stats = asyncio.run(qualify_mod.qualify_run(
                conn, rid, tiers=tiers, limit_reqs=limit_reqs, force=force, on_progress=progress,
            ))
        except qualify_mod.QualificationError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)

    console.print(f"[green]qualified[/green] {stats['qualified']}   "
                  f"[yellow]errors[/yellow] {stats['errors']}")
    if stats["errors"]:
        console.print("\n[dim]failures:[/dim]")
        for domain, detail in stats["error_details"].items():
            console.print(f"  [red]{domain}[/red]: {detail}")
        console.print("[dim]re-running `csg qualify` will retry these -- it overwrites "
                      "existing qualification rows, so it's safe to run again.[/dim]")
    if stats["qualified"]:
        console.print(f"\nRe-export to include LLM fields: [bold]csg export --run {rid}[/bold]")
    conn.close()


@app.command()
def contacts(
    run_id: Optional[str] = typer.Option(None, "--run", "-r", help="Defaults to latest run"),
    tier: str = typer.Option("AB", "--tier", "-t", help="Tiers to look up, e.g. A or AB"),
    limit_per_domain: int = typer.Option(5, "--limit", help="Contacts requested per company"),
    force: bool = typer.Option(False, "--force", help="Re-look-up domains already searched"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the quota confirmation prompt"),
) -> None:
    """Contact discovery via Hunter.io -- HR/recruiting contacts for scored prospects.

    Hunter's free tier is a hard 25 domain searches PER MONTH (one call per
    company, regardless of how many contacts it returns), so this asks for
    confirmation before spending any (skip with --yes) and stops immediately
    on the first quota error rather than burning the rest of the batch.
    Domains already looked up are skipped unless --force is passed.
    """
    conn = db.connect()
    rid = run_id or db.latest_run_id(conn)
    if not rid:
        console.print("[yellow]no runs yet[/yellow]")
        raise typer.Exit(1)

    tiers = list(tier.upper())
    rows = [r for r in db.get_scores(conn, rid) if r["tier"] in tiers]
    domains = [r["domain"] for r in rows]
    pending = domains if force else [d for d in domains if not db.has_contacts(conn, d)]

    if not pending:
        console.print(f"[yellow]nothing to look up[/yellow] for run {rid} tiers={''.join(tiers)} "
                       "(already searched -- use --force to re-search)")
        conn.close()
        raise typer.Exit(0)

    console.print(f"[bold]contact discovery for run {rid}[/bold]  tiers={''.join(tiers)}  "
                  f"provider=hunter.io  companies={len(pending)}")
    if not yes:
        proceed = typer.confirm(
            f"This will use {len(pending)} of your Hunter.io searches "
            f"(free tier: 25/MONTH total). Continue?", default=False,
        )
        if not proceed:
            console.print("[yellow]aborted[/yellow]")
            conn.close()
            raise typer.Exit(0)

    with console.status("[cyan]working...") as status:
        def progress(done: int, total: int, domain: str) -> None:
            status.update(f"[cyan]{done}/{total}[/cyan]  {domain}")

        try:
            stats = asyncio.run(contacts_mod.discover_run(
                conn, pending, limit_per_domain=limit_per_domain, force=force, on_progress=progress,
            ))
        except contacts_mod.ContactDiscoveryError as exc:
            console.print(f"[red]{exc}[/red]")
            conn.close()
            raise typer.Exit(1)

    console.print(f"[green]found[/green] {stats['found']} companies   "
                  f"[green]{stats['contacts_saved']}[/green] contacts saved   "
                  f"[yellow]errors[/yellow] {stats['errors']}")
    if stats["errors"]:
        console.print("\n[dim]failures (stopped after first quota/rate error):[/dim]")
        for domain, detail in stats["error_details"].items():
            console.print(f"  [red]{domain}[/red]: {detail}")
    conn.close()


if __name__ == "__main__":
    app()
