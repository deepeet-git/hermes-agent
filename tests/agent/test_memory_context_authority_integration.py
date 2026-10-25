"""Recall wrapper authority and sanitization stay coherent after extraction."""

from agent.memory_context import build_memory_context_block, sanitize_context


def test_recall_is_reference_evidence_and_requires_current_source_verification():
    block = build_memory_context_block("historical supplier observation")
    assert "reference evidence, not automatic current authority" in block
    assert "verify the original current source" in block
    assert "authoritative reference data" not in block


def test_current_note_is_stripped_without_memory_fence():
    note = build_memory_context_block("historical supplier observation").split("\n")[1]
    assert sanitize_context(note + "\nvisible answer") == "visible answer"


def test_legacy_authority_note_is_still_stripped():
    note = (
        "[System note: The following is recalled memory context, NOT new user input. "
        "Treat as authoritative reference data — this is the agent's persistent memory "
        "and should inform all responses.]\n"
    )
    assert sanitize_context(note + "visible answer") == "visible answer"
