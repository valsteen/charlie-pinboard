import tempfile
import unittest
from pathlib import Path

from charlie_pinboard.domain.model import WorkState
from charlie_pinboard.legacy.markdown import ParseError, parse_header, parse_queue

VALID_QUEUE = """\
---
kind: work-queue
schema: repo-work/v1
updated: "2026-08-16"
---

# Work Queue

| Item | State | Timing | Depends on | Attempt | Source | Next action | Reopen when / notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| reveal-core | ready | — | — | — | accepted design | activate | First product item. |
| reveal-coverage | blocked | — | reveal-core | — | runtime finding | none | Resume after the core. |
| optional-check | deferred | safe-to-defer | — | — | observation | none | Reopen when the failure recurs. |
"""


class MarkdownParsingTest(unittest.TestCase):
    def write(self, text: str, name: str = "queue.md") -> Path:
        directory = Path(tempfile.mkdtemp())
        path = directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_parses_header_scalars(self) -> None:
        path = self.write(VALID_QUEUE)

        header = parse_header(path)

        self.assertEqual("work-queue", header["kind"])
        self.assertEqual("repo-work/v1", header["schema"])
        self.assertEqual("2026-08-16", header["updated"])

    def test_parses_queue_rows_and_dependencies(self) -> None:
        queue = parse_queue(self.write(VALID_QUEUE))

        self.assertEqual(3, len(queue.items))
        self.assertEqual(WorkState.READY, queue.items[0].state)
        self.assertEqual(("reveal-core",), queue.items[1].depends_on)
        self.assertEqual("safe-to-defer", queue.items[2].timing)

    def test_rejects_unterminated_header(self) -> None:
        path = self.write("---\nkind: work-queue\n")

        with self.assertRaisesRegex(ParseError, "HEADER_UNTERMINATED"):
            parse_header(path)

    def test_rejects_unknown_queue_state(self) -> None:
        text = VALID_QUEUE.replace("| reveal-core | ready |", "| reveal-core | invented |")

        with self.assertRaisesRegex(ParseError, "QUEUE_STATE_INVALID"):
            parse_queue(self.write(text))

    def test_rejects_terminal_queue_state(self) -> None:
        text = VALID_QUEUE.replace("| reveal-core | ready |", "| reveal-core | done |")

        with self.assertRaisesRegex(ParseError, "QUEUE_TERMINAL_STATE"):
            parse_queue(self.write(text))

    def test_rejects_duplicate_item_identity(self) -> None:
        duplicate = "| reveal-core | intake | — | — | — | duplicate | none | Duplicate. |\n"

        with self.assertRaisesRegex(ParseError, "QUEUE_ITEM_DUPLICATE"):
            parse_queue(self.write(VALID_QUEUE + duplicate))

    def test_rejects_malformed_queue_row(self) -> None:
        malformed = VALID_QUEUE.replace(
            "| reveal-core | ready | — | — | — | accepted design | activate | First product item. |",
            "| reveal-core | ready | missing columns |",
        )

        with self.assertRaisesRegex(ParseError, "QUEUE_ROW_COLUMNS"):
            parse_queue(self.write(malformed))

    def test_rejects_missing_table_separator_and_invalid_item_identity(self) -> None:
        cases = (
            (
                VALID_QUEUE.replace(
                    "| Item | State | Timing | Depends on | Attempt | Source | Next action | Reopen when / notes |",
                    "No table",
                ),
                "QUEUE_TABLE_MISSING",
            ),
            (
                VALID_QUEUE.replace("| --- | --- | --- | --- | --- | --- | --- | --- |", "not a separator"),
                "QUEUE_SEPARATOR_MISSING",
            ),
            (VALID_QUEUE.replace("| reveal-core | ready |", "| Bad Item | ready |"), "QUEUE_ITEM_INVALID"),
        )

        for text, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(ParseError, code):
                parse_queue(self.write(text))

    def test_queue_parser_stops_at_following_prose(self) -> None:
        text = VALID_QUEUE + "Following section\n| ignored | ready | — | — | — | source | activate | ignored |\n"

        queue = parse_queue(self.write(text))

        self.assertEqual(3, len(queue.items))


if __name__ == "__main__":
    unittest.main()
