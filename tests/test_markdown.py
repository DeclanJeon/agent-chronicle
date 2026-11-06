"""Strict frontmatter, Unicode normalization, and approval digest regressions."""

import unittest

from chronicle.errors import ChronicleError
from chronicle.markdown import approval_digest, parse_frontmatter, render_markdown
from chronicle.schema import parse_json_object


class MarkdownTests(unittest.TestCase):
    def test_digest_normalizes_line_endings_and_unicode(self) -> None:
        composed = render_markdown({"title": "Café"}, "Body\n")
        decomposed = composed.replace("é", "e\u0301")
        self.assertEqual(approval_digest(composed.encode(), 1), approval_digest(decomposed.encode(), 1))
        self.assertEqual(approval_digest(composed.encode(), 1), approval_digest(composed.replace("\n", "\r\n").encode(), 1))

    def test_digest_binds_metadata_and_body_whitespace(self) -> None:
        original = render_markdown({"scope": "project:one"}, "claim\n")
        changed_scope = render_markdown({"scope": "project:two"}, "claim\n")
        changed_space = render_markdown({"scope": "project:one"}, "claim \n")
        digest = approval_digest(original.encode(), 1)
        self.assertNotEqual(digest, approval_digest(changed_scope.encode(), 1))
        self.assertNotEqual(digest, approval_digest(changed_space.encode(), 1))

    def test_rejects_invalid_utf8_bom_duplicate_keys_aliases_and_tags(self) -> None:
        payloads = (
            b"\xff",
            b"\xef\xbb\xbf---\na: b\n---\n",
            b"---\na: 1\na: 2\n---\n",
            b"---\na: &anchor 1\nb: *anchor\n---\n",
            b"---\na: !!python/object/apply:os.system ['echo unsafe']\n---\n",
        )
        for payload in payloads:
            with self.subTest(payload=payload[:20]), self.assertRaises(ChronicleError):
                _ = parse_frontmatter(payload)

    def test_unknown_digest_version_fails_closed(self) -> None:
        with self.assertRaisesRegex(ChronicleError, "unknown_digest_version"):
            _ = approval_digest(b"---\ntitle: test\n---\nbody\n", 2)

    def test_json_configuration_reader_rejects_duplicate_keys_and_non_objects(self) -> None:
        with self.assertRaisesRegex(ChronicleError, "duplicate_json_key"):
            _ = parse_json_object('{"vault":"one","vault":"two"}', "test config")
        with self.assertRaisesRegex(ChronicleError, "invalid_json"):
            _ = parse_json_object('["not", "an object"]', "test config")

    def test_digest_requires_complete_frontmatter(self) -> None:
        with self.assertRaises(ChronicleError):
            _ = approval_digest(b"not markdown", 1)


if __name__ == "__main__":
    _ = unittest.main()
