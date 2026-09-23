"""Conversation Agent (Agent 3) -- owns the pipeline after Sent.

The Outreach Agent's job ends when an email leaves the building. This package
picks it up there and drives the rest of the funnel:

    sent -> waiting -> replied -> booked
                    \-> not_interested ("Failed" in the Dashboard, terminal)

It runs inside the Outreach Agent's own backend process and against the same
Supabase project, reusing its db.py, config.py, models.py, personalize.py and
channels/email.py rather than duplicating any of them. It is nonetheless a
self-contained package: deleting this directory and the four routes in
routes.py would leave the Outreach Agent exactly as it was before Agent 3
existed.

Module map, in the order a reply flows through them:

    settings.py    the per-campaign ON/OFF toggle (and auto_reply, follow-up knobs)
    inbox.py       IMAP: fetch replies from the mailbox outreach was sent from
    matcher.py     map an inbound email back to the outreach_item it answers
    classifier.py  LLM: decide what the reply actually means
    responder.py   compose a reply; send it only if auto_reply is on
    followup.py    nudge prospects who never answered
    engine.py      the tick -- the ONLY thing here that writes a stage

Why it shares the Outreach Agent's database, when Research and Outreach
deliberately do not share one: Research and Outreach exchange *copies* of
data across an HTTP boundary and own separate lifecycles. This agent operates
on the very same outreach_items rows a person watches on the kanban board --
there is nothing to copy, and a second database would mean two sources of
truth for one card's stage. Isolation here is by stage ownership instead (see
models.CONVERSATION_STAGES), which is what actually prevents the two agents
from colliding.

Four invariants hold everywhere in this package:

1. OFF means OFF. With `enabled` false the agent performs no reads of the
   inbox, no sends, and no stage writes. It is re-checked immediately before
   every send, not once per tick.
2. Every write goes through db.set_stage with actor="conversation_agent", so
   the audit trail in outreach_events records the agent's every move exactly
   the way a person's moves are recorded.
3. Stage transitions are forward-only and whitelisted
   (models.CONVERSATION_TRANSITIONS). A bad classification cannot move a card
   backwards or skip a stage.
4. Ingestion is idempotent. Re-reading the same reply is normal and harmless;
   the (item_id, provider_message_id) constraint absorbs it.
"""

from __future__ import annotations
