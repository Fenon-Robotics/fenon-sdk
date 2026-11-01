"""Tests for the comment rules: no ``#`` comments, and no docstring but the module's."""

from __future__ import annotations

from check_comment_rules import findings


def test_a_module_docstring_and_plain_strings_pass() -> None:
    source = b'"""The module."""\n\nNAME = "text"\n\n\ndef run() -> str:\n    return "# not a comment"\n'
    assert findings(source) == []


def test_a_comment_is_reported_with_its_line() -> None:
    source = b'"""The module."""\n\nNAME = 1  # trailing\n# own line\n'
    assert findings(source) == [(3, "comment"), (4, "comment")]


def test_function_method_and_class_docstrings_are_reported() -> None:
    source = (
        b'"""The module."""\n\n\n'
        b'def run() -> None:\n    """Run."""\n\n\n'
        b'async def wait() -> None:\n    """Wait."""\n\n\n'
        b'class Thing:\n    """A thing."""\n\n    def act(self) -> None:\n        """Act."""\n'
    )
    assert [line for line, _ in findings(source)] == [5, 9, 13, 16]


def test_a_string_after_the_first_statement_isnt_a_docstring() -> None:
    source = b'"""The module."""\n\n\ndef run() -> None:\n    value = 1\n    "a stray string"\n'
    assert findings(source) == []
