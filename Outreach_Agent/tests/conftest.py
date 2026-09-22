"""Shared fixtures: a fake in-memory PostgREST, same pattern as the Research
Agent's tests/test_db_roundtrip.py -- runs offline, still exercises the real
db.py code paths."""

from __future__ import annotations

from typing import Any

import pytest

_KEYS = {
    "prospects": ("research_domain",),
    "campaigns": ("campaign_id",),
    "outreach_items": ("item_id",),
    "outreach_events": ("event_id",),
    "conversation_settings": ("campaign_id",),
    "conversation_messages": ("message_id",),
    "draft_versions": ("version_id",),
    "learning_insights": ("pattern_key",),
    "learning_events": ("event_id",),
}

_UNIQUE = {
    # (campaign_id, research_domain) uniqueness on outreach_items, enforced by
    # create_item's on_conflict -- mirrored here so upsert-by-alt-key works.
    "outreach_items": ("campaign_id", "research_domain"),
}


def _as_text(value: Any) -> str:
    """Render a value the way PostgREST would compare it in a filter --
    booleans as lowercase 'true'/'false', everything else via str()."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class FakeConn:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}

    def _pk(self, table: str, row: dict[str, Any]) -> tuple:
        keys = _KEYS.get(table, ("id",))
        return tuple(row.get(k) for k in keys)

    def _alt_key(self, table: str, row: dict[str, Any]) -> tuple | None:
        keys = _UNIQUE.get(table)
        return tuple(row.get(k) for k in keys) if keys else None

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str = "") -> list[dict[str, Any]]:
        store = self.tables.setdefault(table, [])
        out = []
        for row in rows:
            pk = self._pk(table, row)
            alt = self._alt_key(table, row)
            for i, existing in enumerate(store):
                if self._pk(table, existing) == pk or (alt and self._alt_key(table, existing) == alt):
                    store[i] = dict(row)
                    out.append(store[i])
                    break
            else:
                store.append(dict(row))
                out.append(row)
        return out

    def insert(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        store = self.tables.setdefault(table, [])
        store.extend(dict(r) for r in rows)
        return rows

    def select(self, table: str, select: str = "*", limit: int | None = None,
               order: str | None = None, **filters: str) -> list[dict[str, Any]]:
        rows = self.tables.get(table, [])
        for field, expr in filters.items():
            if expr.startswith("eq."):
                want = expr[3:]
                # PostgREST compares booleans as 'true'/'false' text; Python
                # stringifies them as 'True'/'False'. Normalize so the
                # conversation agent's enabled=eq.true filters work here.
                rows = [r for r in rows if _as_text(r.get(field)) == want]
            elif expr.startswith("in."):
                wanted = set(expr[3:].strip("()").split(","))
                rows = [r for r in rows if str(r.get(field)) in wanted]
            elif expr == "is.null":
                rows = [r for r in rows if r.get(field) is None]
            elif expr == "not.is.null":
                rows = [r for r in rows if r.get(field) is not None]
            elif expr.startswith("lt."):
                want = expr[3:]
                rows = [r for r in rows if r.get(field) is not None and str(r.get(field)) < want]
            elif expr.startswith("gt."):
                want = expr[3:]
                rows = [r for r in rows if r.get(field) is not None and str(r.get(field)) > want]
            elif expr.startswith("cs."):
                # PostgREST's JSON/array "contains" operator, e.g.
                # `research_run_ids=cs.["disc-A"]` -- matches rows whose
                # `field` (a Python list, since JSONB round-trips as one
                # here) contains every element of the query's array. Only
                # single-element containment checks are needed by this
                # codebase (list_prospects filters on one run_id at a time),
                # so a minimal json.loads + subset check is enough.
                import json as _json
                wanted = set(_json.loads(expr[3:]))
                rows = [r for r in rows if wanted.issubset(set(r.get(field) or []))]
        if order:
            field = order.split(".")[0]
            desc = "desc" in order
            rows = sorted(rows, key=lambda r: (r.get(field) is None, r.get(field)), reverse=desc)
        return rows[:limit] if limit else list(rows)

    def update(self, table: str, values: dict[str, Any], **filters: str) -> None:
        for row in self.select(table, **filters):
            row.update(values)

    def update_returning(self, table: str, values: dict[str, Any], **filters: str) -> list[dict[str, Any]]:
        """PATCH reporting which rows it matched -- the real SupabaseConn's
        `Prefer: return=representation`. The conversation agent's advisory
        lock is a conditional PATCH whose whole point is distinguishing "I
        won the lock" from "someone else holds it", so the fake has to model
        the matched-row count, not just the write."""
        matched = self.select(table, **filters)
        for row in matched:
            row.update(values)
        return [dict(r) for r in matched]

    def delete(self, table: str, **filters: str) -> None:
        rows = self.select(table, **filters)
        store = self.tables.setdefault(table, [])
        self.tables[table] = [r for r in store if r not in rows]

    def commit(self) -> None: ...
    def close(self) -> None: ...


@pytest.fixture
def conn():
    return FakeConn()
