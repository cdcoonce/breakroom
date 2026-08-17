from pathlib import Path

DOC_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "dev-cycle"
    / "issue-authoring-conventions.md"
)


def _doc_text() -> str:
    return DOC_PATH.read_text()


def _normalized() -> str:
    return " ".join(_doc_text().split())


def test_convention_doc_exists() -> None:
    assert DOC_PATH.is_file()


def test_doc_requires_explicit_existing_test_file_list_or_none() -> None:
    text = _normalized()
    assert "Existing test files must be enumerated in Scope" in text
    assert (
        "must include an explicit list of every **existing** test file the "
        "issue authorizes editing" in text
    )
    assert 'say so explicitly (e.g. "Existing test files: none")' in text


def test_doc_names_prior_scope_gate_false_positives() -> None:
    text = _doc_text()
    assert "#94" in text
    assert "test_storylets.py" in text
    assert "#93" in text
    assert "tests/test_tick_norms.py" in text


def test_doc_covers_control_flow_invalidation_case() -> None:
    text = _normalized()
    assert "a production rewrite invalidates existing test assertions" in text.lower()
    assert "loop cardinality" in text
    assert "hardcoded seeds" in text
    assert "indices" in text
