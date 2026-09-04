"""Guard tests for the main.py CLI.

These catch the class of bug where the argparse parser itself is malformed
(e.g. two --limit options), which raises at construction and crashes EVERY
invocation — including `--serve`, taking the whole service down — yet never
shows up in the module-level unit tests that import functions directly.
"""

from __future__ import annotations

import sys


def test_cli_parser_builds_without_conflicts(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py"])
    import main

    ns = main._parse_args()                 # raises ArgumentError on a dup option
    # A few flags we rely on should be present exactly once.
    assert hasattr(ns, "rewrite_seo")
    assert hasattr(ns, "apply")
    assert hasattr(ns, "limit")


def test_rewrite_seo_flags_parse(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["main.py", "--rewrite-seo", "--apply",
                                      "--limit", "5"])
    import main

    ns = main._parse_args()
    assert ns.rewrite_seo is True and ns.apply is True and ns.limit == 5
