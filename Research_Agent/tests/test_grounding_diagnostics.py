"""P0 diagnostics: distinguish absent grounding metadata from an empty object.

The deployed symptom was ``grounding_metadata_count=1`` with zero chunks and
zero supports, while a local run had produced chunks. These tests pin the
shape that separates the two cases so a future log line is unambiguous.
"""

from __future__ import annotations

from types import SimpleNamespace

from csg.discovery import gemini_keys, research


def _response(metadata):
    candidate = SimpleNamespace()
    if metadata is not None:
        candidate.grounding_metadata = metadata
    return SimpleNamespace(candidates=[candidate])


def test_absent_metadata_is_distinct_from_an_empty_metadata_object():
    absent = research._gemini_grounding_shape(_response(None))
    empty = research._gemini_grounding_shape(_response(SimpleNamespace()))

    assert absent["grounding_metadata_count"] == 0
    assert absent["grounding_metadata_present"] is False
    assert absent["grounding_metadata_empty"] is False
    assert empty["grounding_metadata_count"] == 1
    assert empty["grounding_metadata_present"] is True
    assert empty["grounding_metadata_empty"] is True
    assert research._gemini_grounding_unavailable_reason(absent) == (
        "grounding metadata absent from response"
    )
    assert "present but empty" in research._gemini_grounding_unavailable_reason(empty)


def test_metadata_with_only_search_queries_reports_the_queries_it_ran():
    metadata = SimpleNamespace(
        web_search_queries=["acme headquarters", "acme leadership"],
        grounding_chunks=[],
        grounding_supports=[],
    )
    shape = research._gemini_grounding_shape(_response(metadata))

    assert shape["web_search_query_count"] == 2
    assert shape["grounding_metadata_empty"] is True
    assert shape["grounding_metadata_keys"] == [
        "grounding_chunks", "grounding_supports", "web_search_queries",
    ]
    reason = research._gemini_grounding_unavailable_reason(shape)
    assert "queries=2" in reason


def test_metadata_with_chunks_but_no_supports_is_named_separately():
    metadata = {"groundingChunks": [{"web": {"uri": "https://acme.com/about"}}]}
    shape = research._gemini_grounding_shape(_response(metadata))

    assert shape["grounding_metadata_empty"] is False
    assert shape["grounding_chunk_count"] == 1
    assert research._gemini_grounding_unavailable_reason(shape) == (
        "grounding metadata present without support segments"
    )


def test_client_profile_carries_sdk_identity_without_secrets():
    profile = gemini_keys.gemini_client_profile()

    assert profile["gemini_sdk"] == "google-genai"
    assert isinstance(profile["gemini_sdk_version"], str) and profile["gemini_sdk_version"]
    assert profile["gemini_api_mode"] == "enterprise"
    assert "api_key" not in profile


def test_support_with_only_character_offsets_recovers_segment_text():
    text = "Acme employs 500 people across three offices."
    start = text.index("Acme")
    end = text.index(".") + 1
    metadata = {
        "groundingChunks": [{"web": {"uri": "https://acme.com/about"}}],
        "groundingSupports": [{
            "segment": {"startIndex": start, "endIndex": end},
            "groundingChunkIndices": ["0"],
        }],
    }
    response = SimpleNamespace(candidates=[SimpleNamespace(grounding_metadata=metadata)], text=text)

    assert research._extract_gemini_grounding_supports(response) == {
        "https://acme.com/about": [text[start:end]],
    }
    shape = research._gemini_grounding_shape(response)
    assert shape["grounding_support_count"] == 1
    assert shape["grounding_supported_url_count"] == 1
