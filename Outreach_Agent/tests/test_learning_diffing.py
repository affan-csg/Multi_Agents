"""Pure, table-driven tests for learning/diffing.py -- no DB, no LLM. Every
flag is derived from plain text measurements; these tests pin down the exact
expected value for concrete before/after string pairs."""

from __future__ import annotations

from outreach.learning.diffing import compute_diff

_SIGNATURE = "\n\nBest,\nAffan Jan\nCareer Source Group LLC\nAlpharetta, Georgia\nhttps://careersourcegroup.com/"


def _body(text: str) -> str:
    return text + _SIGNATURE


def test_subject_changed_true():
    diff = compute_diff("Hiring support for Acme", "x", "Quick question for Acme", "x")
    assert diff["subject_changed"] is True


def test_subject_changed_false_ignores_whitespace():
    diff = compute_diff("  Hiring support  ", "x", "Hiring support", "x")
    assert diff["subject_changed"] is False


def test_opening_changed_true():
    before = _body("Hi there. I noticed Acme has open roles. Worth a quick call?")
    after = _body("Hello! Acme's hiring caught my eye. Worth a quick call?")
    diff = compute_diff("s", before, "s", after)
    assert diff["opening_changed"] is True


def test_opening_changed_false_when_first_sentence_identical():
    before = _body("Hi there. I noticed Acme has open roles. Worth a quick call?")
    after = _body("Hi there. Acme's hiring caught my eye. Worth a quick call?")
    diff = compute_diff("s", before, "s", after)
    assert diff["opening_changed"] is False


def test_cta_changed_and_shortened_excludes_signature():
    before = _body("Hi there. Great fit. Would you be available for a brief 15-minute conversation next week?")
    after = _body("Hi there. Great fit. Open to a quick chat next week?")
    diff = compute_diff("s", before, "s", after)
    assert diff["cta_changed"] is True
    assert diff["cta_shortened"] is True


def test_cta_unchanged_when_only_signature_differs():
    before = "Hi there. Worth a quick call?\n\nBest,\nAffan Jan\nCareer Source Group LLC"
    after = "Hi there. Worth a quick call?\n\nBest,\nAffan Jan\nCareer Source Group LLC\nAlpharetta, Georgia"
    diff = compute_diff("s", before, "s", after)
    assert diff["cta_changed"] is False
    assert diff["cta_shortened"] is False


def test_cta_lengthened_not_flagged_as_shortened():
    before = _body("Hi there. Worth a quick call?")
    after = _body("Hi there. Would you have any availability for a brief fifteen minute introductory conversation sometime next week?")
    diff = compute_diff("s", before, "s", after)
    assert diff["cta_changed"] is True
    assert diff["cta_shortened"] is False


def test_shortened_flag_on_material_word_reduction():
    before = _body("This is a fairly long email with quite a lot of extra padding words in the body text here.")
    after = _body("Short email now.")
    diff = compute_diff("s", before, "s", after)
    assert diff["shortened"] is True
    assert diff["lengthened"] is False
    assert diff["length_delta_words"] < 0


def test_lengthened_flag_on_material_word_increase():
    before = _body("Short email.")
    after = _body("This is a much longer email with quite a lot of additional detail and padding words added in.")
    diff = compute_diff("s", before, "s", after)
    assert diff["lengthened"] is True
    assert diff["shortened"] is False
    assert diff["length_delta_words"] > 0


def test_small_delta_is_noise_not_shortened_or_lengthened():
    before = _body("Hi there, worth a quick call soon?")
    after = _body("Hi, worth a quick call soon?")
    diff = compute_diff("s", before, "s", after)
    assert diff["shortened"] is False
    assert diff["lengthened"] is False


def test_sentences_removed_count():
    before = _body("First sentence. Second sentence. Third sentence. Worth a call?")
    after = _body("First sentence. Worth a call?")
    diff = compute_diff("s", before, "s", after)
    assert diff["sentences_removed_count"] == 2


def test_sentences_removed_count_floors_at_zero_when_sentences_added():
    before = _body("Worth a call?")
    after = _body("First sentence. Second sentence. Worth a call?")
    diff = compute_diff("s", before, "s", after)
    assert diff["sentences_removed_count"] == 0


def test_personalization_delta_positive_when_tokens_added():
    prospect = {"contact_name": "Jordan", "name": "Acme Corp", "research_domain": "acme.com"}
    before = _body("Hi there, worth a quick call?")
    after = _body("Hi Jordan, saw Acme Corp is growing -- worth a quick call?")
    diff = compute_diff("s", before, "s", after, prospect)
    assert diff["personalization_delta"] > 0


def test_personalization_delta_negative_when_tokens_removed():
    prospect = {"contact_name": "Jordan", "name": "Acme Corp", "research_domain": "acme.com"}
    before = _body("Hi Jordan, saw Acme Corp is growing -- worth a quick call?")
    after = _body("Hi there, worth a quick call?")
    diff = compute_diff("s", before, "s", after, prospect)
    assert diff["personalization_delta"] < 0


def test_personalization_delta_zero_without_prospect():
    before = _body("Hi Jordan, worth a call?")
    after = _body("Hi there, worth a call?")
    diff = compute_diff("s", before, "s", after, prospect=None)
    assert diff["personalization_delta"] == 0


def test_formatting_changed_when_bullets_added():
    before = _body("Here is why: we can help with staffing.")
    after = _body("Here is why we can help:\n- Staffing\n- Recruiting\n- Search")
    diff = compute_diff("s", before, "s", after)
    assert diff["formatting_changed"] is True


def test_formatting_unchanged_for_similar_structure():
    before = _body("Line one.\nLine two.")
    after = _body("Line uno.\nLine dos.")
    diff = compute_diff("s", before, "s", after)
    assert diff["formatting_changed"] is False


def test_handles_none_before():
    diff = compute_diff(None, None, "Subject", _body("Hi there, worth a call?"))
    assert diff["subject_changed"] is True
    assert diff["sentences_removed_count"] == 0


def test_handles_none_after():
    diff = compute_diff("Subject", _body("Hi there, worth a call?"), None, None)
    assert diff["subject_changed"] is True


def test_handles_empty_strings():
    diff = compute_diff("", "", "", "")
    assert diff["subject_changed"] is False
    assert diff["cta_changed"] is False
    assert diff["formatting_changed"] is False


def test_diff_version_stamped():
    diff = compute_diff("s", "b", "s", "b")
    assert diff["diff_version"] == 1
