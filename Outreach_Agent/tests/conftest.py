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
}

_UNIQUE = {
    # (campaign_id, research_domain) uniqueness on outreach_items, enforced by
    # create_item's on_conflict -- mirrored here so upsert-by-alt-key works.
    "outreach_items": ("campaign_id", "research_domain"),
}


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
                rows = [r for r in rows if str(r.get(field)) == want]
            elif expr.startswith("in."):
                wanted = set(expr[4:-1].split(","))
                rows = [r for r in rows if str(r.get(field)) in wanted]
        if order:
            field = order.split(".")[0]
            desc = "desc" in order
            rows = sorted(rows, key=lambda r: (r.get(field) is None, r.get(field)), reverse=desc)
        return rows[:limit] if limit else list(rows)

    def update(self, table: str, values: dict[str, Any], **filters: str) -> None:
        for row in self.select(table, **filters):
            row.update(values)

    def delete(self, table: str, **filters: str) -> None:
        rows = self.select(table, **filters)
        store = self.tables.setdefault(table, [])
        self.tables[table] = [r for r in store if r not in rows]

    def commit(self) -> None: ...
    def close(self) -> None: ...


@pytest.fixture
def conn():
    return FakeConn()
