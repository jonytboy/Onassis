"""Tests for Pinterest auto-promotion (publishing pins for live products)."""

from __future__ import annotations

from onassis.connectors.pinterest import PinterestConnector


class StubPinClient:
    """Records created pins; can be told to fail on a given link."""

    def __init__(self, fail_link=None):
        self.pins = []
        self.fail_link = fail_link

    def create_pin(self, *, board_id, title, description, link, image_path, alt_text=None):
        if link == self.fail_link:
            raise RuntimeError("pinterest boom")
        self.pins.append({"board_id": board_id, "title": title, "link": link})
        return {"id": f"pin_{len(self.pins)}"}


def _config(config, **over):
    config.pinterest = {"access_token": "tok", "board_id": "board123",
                        "max_pins_per_product": 3, **over}
    return config


def _pins(n=2, link="https://www.etsy.com/listing/555"):
    return [{"title": f"Pin {i}", "description": "d", "link": link,
             "image_path": None, "alt_text": "a"} for i in range(n)]


def test_publishes_pins_to_the_board(config):
    client = StubPinClient()
    conn = PinterestConnector(_config(config), pin_client=client)
    result = conn.publish_pins(_pins(2))
    assert result["posted"] == 2 and result["failed"] == 0
    assert all(p["board_id"] == "board123" for p in client.pins)
    assert all(p["link"] == "https://www.etsy.com/listing/555" for p in client.pins)


def test_respects_max_pins_per_product(config):
    client = StubPinClient()
    conn = PinterestConnector(_config(config, max_pins_per_product=2), pin_client=client)
    result = conn.publish_pins(_pins(5))
    assert result["posted"] == 2 and result["skipped"] == 3   # capped


def test_one_pin_failure_is_not_fatal(config):
    link = "https://www.etsy.com/listing/900"
    client = StubPinClient(fail_link=link)
    conn = PinterestConnector(_config(config), pin_client=client)
    result = conn.publish_pins(_pins(2, link=link))
    assert result["failed"] == 2 and result["posted"] == 0    # both target the bad link
    assert result["results"][0]["status"] == "failed"


def test_no_op_when_not_configured(config):
    config.pinterest = {}   # no token / board
    conn = PinterestConnector(config)
    assert conn.can_publish is False
    result = conn.publish_pins(_pins(2))
    assert result["posted"] == 0 and result["skipped"] == 2


def test_can_publish_requires_token_and_board(config):
    assert PinterestConnector(_config(config)).can_publish is True
    config.pinterest = {"access_token": "tok"}   # no board
    assert PinterestConnector(config).can_publish is False
