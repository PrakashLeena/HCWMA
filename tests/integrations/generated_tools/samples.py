"""A small, valid generated tool used by the Step 13 tests and the proof."""

from __future__ import annotations

MANIFEST = """\
name: word_count
version: 0.1.0
created_by: tool_builder
status: quarantined
route: local_data
description: Count the words, lines and characters in a text.
inputs:
  text: string
outputs:
  words: integer
  lines: integer
  characters: integer
permissions:
  network: []
  filesystem: []
  secrets: []
side_effects: none
approval_required: false
"""

ADAPTER = """\
import re


def run(args, ctx):
    text = args["text"]
    words = len(re.findall(r"\\S+", text))
    lines = len(text.splitlines())
    return {"words": words, "lines": lines, "characters": len(text)}
"""

TESTS = """\
import unittest

from adapter import run


class WordCountTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(run({"text": ""}, None), {"words": 0, "lines": 0, "characters": 0})

    def test_normal(self):
        out = run({"text": "one two  three\\nfour"}, None)
        self.assertEqual(out["words"], 4)
        self.assertEqual(out["lines"], 2)


if __name__ == "__main__":
    unittest.main()
"""

NETWORK_MANIFEST = """\
name: page_title
version: 0.1.0
created_by: tool_builder
status: quarantined
route: custom_api
description: Return the first heading text of a page on example.org.
inputs:
  path: string
outputs:
  title: string
permissions:
  network: [example.org]
  filesystem: []
  secrets: []
side_effects: none
approval_required: false
"""

NETWORK_ADAPTER = """\
import re


def run(args, ctx):
    page = ctx.fetch("https://example.org/" + args["path"])
    found = re.search(r"<h1>(.*?)</h1>", page["text"])
    return {"title": found.group(1) if found else ""}
"""

NETWORK_TESTS = """\
import unittest

from adapter import run


class Fake:
    def fetch(self, url):
        return {"status": 200, "final_url": url, "text": "<h1>Hi</h1>"}


class TitleTests(unittest.TestCase):
    def test_title(self):
        self.assertEqual(run({"path": "x"}, Fake()), {"title": "Hi"})
"""
