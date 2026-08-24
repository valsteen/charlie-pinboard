import json
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from charlie_pinboard.interfaces.transition_input import TransitionInputError, parse_transition_input
from tests.support import JsonValue


class ParseProperties(unittest.TestCase):
    @settings(max_examples=50)
    @given(invalid=st.one_of(st.none(), st.integers(), st.lists(st.integers()), st.dictionaries(st.text(), st.text())))
    def test_transition_input_rejects_non_string_attempt(self, invalid: JsonValue) -> None:
        value: dict[str, JsonValue] = {
            "attempt": invalid,
            "branch": "codex/reveal-core",
            "base_revision": "abc123",
            "owner": "worker",
            "brief_artifact_ref_id": 1,
        }
        with self.assertRaises(TransitionInputError):
            parse_transition_input("activate", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
