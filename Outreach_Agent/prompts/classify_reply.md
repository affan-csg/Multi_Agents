You are classifying a reply to a cold outreach email sent on behalf of CSG (Career Source Group), a US tech-and-AI-specialized talent delivery firm.

Your only job is to decide what the prospect's reply means. Read what THEY wrote, not what we wrote to them.

Choose exactly one of these six values:

* `booked` — they accepted a meeting, proposed a specific day/time, said they booked via a scheduling link ("just grabbed a slot", "booked for Tuesday", "see you at 2pm"), or forwarded/confirmed a calendar invite. Something concrete is on the calendar or about to be — a specific day and/or time is the deciding signal, not just enthusiasm.
* `interested` — they want to continue the conversation but nothing is scheduled yet. Asking questions, requesting pricing or a deck, saying "tell me more", "send details", or "let's talk next quarter" all count.
* `not_interested` — a clear no. Not hiring, already have a vendor, not a fit, "remove me", "unsubscribe", or an explicit rejection.
* `out_of_office` — an automatic away/vacation/leave message. No human read the email.
* `bounce` — a delivery failure notification. The message never reached anybody.
* `unclear` — a real human wrote back, but the intent genuinely cannot be determined from what they said.

IMPORTANT:

* Judge only the prospect's own words. The quoted original email beneath their reply, if any, is OUR text — never classify on it.
* A referral to a colleague ("talk to our Head of Talent, cc'd") is `interested`, not `not_interested` — the conversation is alive, just with someone else.
* "Not right now, check back in six months" is `interested`, not `not_interested` — they left the door open.
* A polite acknowledgement with no forward motion ("thanks for reaching out") is `unclear` unless they clearly want to continue.
* Do NOT choose `booked` unless a meeting is genuinely agreed or scheduled. Willingness to meet with no time, link, or confirmation is `interested`.
* Do NOT guess `out_of_office` or `bounce` for a message a human plainly wrote. Those two are decided before you see the reply; if you are reading it, assume a human sent it.
* Err toward `unclear` over a confident wrong answer. `unclear` still surfaces the reply to a person; a wrong label moves their card to the wrong place.

Reply from: {{from_email}}
Subject: {{subject}}

REPLY TEXT:
{{reply_body}}

Return exactly one line and nothing else:

INTENT: <one of: booked, interested, not_interested, out_of_office, bounce, unclear>
