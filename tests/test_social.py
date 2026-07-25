"""Tests for the Meta (Facebook) connector — video posting + token minting."""

from __future__ import annotations

from types import SimpleNamespace

from onassis.connectors.social import FacebookPublisher, mint_page_token


class _FakeMeta:
    def __init__(self):
        self.calls = []

    def page_video(self, page_id, url, desc):
        self.calls.append(("video", page_id, url, desc))
        return {"id": "v1"}

    def page_feed(self, page_id, msg, link):
        self.calls.append(("feed", page_id, msg, link))
        return {"id": "f1"}

    def exchange_long_lived(self, app_id, secret, short):
        return "LONG_USER" if short == "SHORT" else ""

    def list_pages(self, user_token):
        return [{"id": "111", "name": "Onassis Med", "access_token": "PERMA_TOKEN"},
                {"id": "222", "name": "Other", "access_token": "x"}]


def _fb(**meta):
    cfg = SimpleNamespace(meta={"page_access_token": "t", "facebook_page_id": "111",
                                **meta})
    return FacebookPublisher(cfg, client=_FakeMeta())


def test_post_video_hits_page_video():
    fb = _fb()
    res = fb.post_video("https://cdn/x.mp4", "Mug ✨")
    assert res["ok"] and res["ref"] == "v1"
    assert fb._c().calls[0] == ("video", "111", "https://cdn/x.mp4", "Mug ✨")


def test_mint_page_token_returns_permanent_page_token():
    r = mint_page_token("app", "secret", "SHORT", page_id="111", client=_FakeMeta())
    assert r["ok"] and r["page_token"] == "PERMA_TOKEN"
    assert r["page_name"] == "Onassis Med" and r["page_id"] == "111"


def test_mint_page_token_lists_pages_when_no_match():
    r = mint_page_token("app", "secret", "SHORT", page_id="999", client=_FakeMeta())
    assert r["ok"] is False
    assert {p["name"] for p in r["pages"]} == {"Onassis Med", "Other"}
