"""Pure text-classification tests for strategy.compute_strategy -- no DB, no
LLM. Verifies every attribute is derived observably (word count, presence of
a token/phrase) and degrades gracefully on missing/empty input."""

from __future__ import annotations

from outreach.optimization.strategy import compute_strategy
from test_optimization_fixtures import DIRECT_CTA, LONG_BODY_TMPL, SHORT_BODY_TMPL, SOFT_CTA


def test_direct_vs_soft_cta_classified_correctly():
    direct = compute_strategy("i1", "c1", subject="Q", body=SHORT_BODY_TMPL.format(cta=DIRECT_CTA), personalized_from="pain_summary")
    soft = compute_strategy("i2", "c1", subject="Q", body=SHORT_BODY_TMPL.format(cta=SOFT_CTA), personalized_from="pain_summary")
    assert direct.cta_style == "direct"
    assert soft.cta_style == "soft"


def test_short_vs_long_length_bucket():
    short = compute_strategy("i1", "c1", subject="Q", body=SHORT_BODY_TMPL.format(cta=SOFT_CTA), personalized_from="pain_summary")
    long_ = compute_strategy("i2", "c1", subject="Q", body=LONG_BODY_TMPL.format(cta=SOFT_CTA), personalized_from="pain_summary")
    assert short.length_bucket == "short"
    assert long_.length_bucket in ("medium", "long")
    assert long_.word_count > short.word_count


def test_subject_style_question_vs_statement():
    question = compute_strategy("i1", "c1", subject="Are you hiring right now?", body="b", personalized_from=None)
    statement = compute_strategy("i2", "c1", subject="Quick note on your hiring", body="b", personalized_from=None)
    assert question.subject_style == "question"
    assert statement.subject_style == "statement"


def test_personalization_level_from_personalized_from():
    deep = compute_strategy("i1", "c1", subject="Q", body="b", personalized_from="pain_summary")
    none_ = compute_strategy("i2", "c1", subject="Q", body="b", personalized_from="template_only")
    assert deep.personalization_level == "deep"
    assert none_.personalization_level == "none"


def test_email_style_from_personalized_from():
    research_backed = compute_strategy("i1", "c1", subject="Q", body="b", personalized_from="recommended_angle")
    template_only = compute_strategy("i2", "c1", subject="Q", body="b", personalized_from="template_only")
    assert research_backed.email_style == "research_backed"
    assert template_only.email_style == "template_only"


def test_handles_missing_subject_and_body_gracefully():
    profile = compute_strategy("i1", "c1", subject=None, body=None, personalized_from=None)
    assert profile.word_count == 0
    assert profile.length_bucket == "short"
    assert profile.cta_style == "none"
    assert profile.personalization_level == "none"


def test_opening_style_personalized_fact_when_prospect_token_present():
    body = "Acme Corp has been growing fast this year. Let me know if you're open to a chat."
    profile = compute_strategy(
        "i1", "c1", subject="Q", body=body, personalized_from="pain_summary",
        prospect={"research_domain": "acme corp"},
    )
    assert profile.opening_style == "personalized_fact"
