#!/usr/bin/env python3
"""Pruebas de las cuentas recordadas y su ranking."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import xrepost_accounts as accounts


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xrepost-accounts-")
        self.root = Path(self.tmp.name)
        self.accounts = self.root / "accounts.json"
        self.history = self.root / "history.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_split_handles(self):
        self.assertEqual(
            accounts.split_handles("@Ana, luis ,ANA, mal-handle, ,x"),
            ["Ana", "luis", "x"],
        )
        self.assertEqual(accounts.split_handles(["@uno", "dos"]), ["uno", "dos"])
        self.assertEqual(accounts.split_handles(""), [])

    def test_add_and_rank_prefix_and_substring(self):
        self.assertTrue(accounts.add_accounts(["@ETECSA_Cuba", "etecsa_news", "otro"], self.accounts))
        entries = accounts.load_accounts(self.accounts, self.history)
        self.assertEqual(len(entries), 3)

        found = accounts.rank_accounts("etec", entries)
        self.assertEqual([item["handle"] for item in found[:2]], ["ETECSA_Cuba", "etecsa_news"])

        found = accounts.rank_accounts("news", entries)
        self.assertEqual([item["handle"] for item in found], ["etecsa_news"])

        found = accounts.rank_accounts("etecsa_cuba", entries)
        self.assertEqual(found[0]["handle"], "ETECSA_Cuba")

    def test_ranking_prefers_recent_and_excludes_used(self):
        self.accounts.write_text(
            json.dumps(
                {
                    "version": 1,
                    "accounts": [
                        {"handle": "ana", "count": 9, "last_used": "2026-01-01T00:00:00+00:00"},
                        {"handle": "anabel", "count": 1, "last_used": "2026-10-01T00:00:00+00:00"},
                        {"handle": "otro", "count": 50, "last_used": "2026-10-01T00:00:00+00:00"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        entries = accounts.load_accounts(self.accounts, self.history)
        found = accounts.rank_accounts("an", entries)
        self.assertEqual([item["handle"] for item in found], ["anabel", "ana"])

        found = accounts.rank_accounts("", entries, exclude={"otro"})
        self.assertEqual(found[0]["handle"], "anabel")
        self.assertNotIn("otro", [item["handle"] for item in found])

    def test_history_bootstraps_accounts(self):
        events = [
            {"ts": "2026-10-01T10:00:00Z", "action": "repost", "search_user": "ETECSA_Cuba"},
            {"ts": "2026-10-02T10:00:00Z", "action": "repost", "search_user": "ETECSA_Cuba"},
            {"ts": "2026-10-03T10:00:00Z", "action": "undo", "search_user": "Yury"},
            {"ts": "2026-10-04T10:00:00Z", "action": "repost", "search_user": ""},
        ]
        self.history.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        entries = accounts.load_accounts(self.accounts, self.history)
        self.assertEqual(set(entries), {"etecsa_cuba", "yury"})
        self.assertEqual(entries["etecsa_cuba"]["count"], 2)

        found = accounts.suggest("et", self.accounts, self.history)
        self.assertEqual(found[0]["handle"], "ETECSA_Cuba")
        self.assertEqual(found[0]["count"], 2)

    def test_add_increments_and_keeps_case(self):
        accounts.add_accounts("Ana", self.accounts)
        accounts.add_accounts("@ana", self.accounts)
        data = json.loads(self.accounts.read_text(encoding="utf-8"))
        self.assertEqual(data["accounts"][0]["handle"], "ana")
        self.assertEqual(data["accounts"][0]["count"], 2)
        self.assertEqual(len(data["accounts"]), 1)

    def test_replace_token_preserves_separators(self):
        self.assertEqual(
            accounts._replace_token("@ana, @lu", "luis"),
            "@ana, @luis",
        )
        self.assertEqual(
            accounts._replace_token("@ana, @lu", "luis", add_comma=True),
            "@ana, @luis, ",
        )
        self.assertEqual(
            accounts._replace_token("  @an", "ana"),
            "  @ana",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
