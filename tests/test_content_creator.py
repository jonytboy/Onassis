"""Tests for the Content Creator agent (LLM mocked)."""

from __future__ import annotations

import logging

import pytest

from onassis.agents.content_creator import ContentCreator, _content_schema
from tests.conftest import FakeLLM, make_content_response


def _targets(config):
    t = config.content_targets
    return (
        t.get("pinterest_posts", 5),
        t.get("instagram_captions", 3),
        t.get("facebook_posts", 2),
        t.get("image_prompts", 3),
    )


@pytest.fixture
def creator(config, db):
    return ContentCreator(config, db)


def _with_brief(db):
    return db.insert_brief({"brief_date": "2026-06-26", "theme": "T", "keywords": ["k"]})


def test_run_generates_configured_counts(creator, config, db, sample_brief):
    n_pin, n_ig, n_fb, n_img = _targets(config)
    creator._llm = FakeLLM(make_content_response(n_pin, n_ig, n_fb, n_img))

    sample_brief["id"] = _with_brief(db)
    items = creator.run(brief=sample_brief)

    assert len(items) == n_pin + n_ig + n_fb + n_img


def test_run_persists_items_with_correct_platforms(creator, config, db, sample_brief):
    n_pin, n_ig, n_fb, n_img = _targets(config)
    creator._llm = FakeLLM(make_content_response(n_pin, n_ig, n_fb, n_img))
    sample_brief["id"] = _with_brief(db)

    creator.run(brief=sample_brief)
    stored = db.get_content_for_brief(sample_brief["id"])

    counts: dict[str, int] = {}
    for item in stored:
        counts[item["platform"]] = counts.get(item["platform"], 0) + 1
    assert counts == {
        "pinterest": n_pin,
        "instagram": n_ig,
        "facebook": n_fb,
        "image": n_img,
    }


def test_mapping_shapes(creator, config, db, sample_brief):
    # Target one of each so the model's single image prompt is on-target
    # (the count guarantee is exercised separately below).
    config.content_targets = {
        "pinterest_posts": 1, "instagram_captions": 1,
        "facebook_posts": 1, "image_prompts": 1,
    }
    creator._llm = FakeLLM(make_content_response(1, 1, 1, 1))
    sample_brief["id"] = _with_brief(db)
    items = creator.run(brief=sample_brief)

    by_platform = {i["platform"]: i for i in items}
    assert by_platform["pinterest"]["content_type"] == "post"
    assert by_platform["pinterest"]["body"] == "desc 0"
    assert by_platform["pinterest"]["metadata"]["hashtags"] == ["#a", "#b"]
    assert by_platform["instagram"]["content_type"] == "caption"
    assert by_platform["instagram"]["body"] == "caption 0"
    assert by_platform["facebook"]["content_type"] == "post"
    assert by_platform["image"]["content_type"] == "image_prompt"
    assert by_platform["image"]["body"] == "prompt 0"
    assert by_platform["image"]["metadata"]["aspect_ratio"] == "4:5"


def test_image_prompts_shortfall_is_recovered(creator, config, db, sample_brief):
    # The model returns only 1 image prompt but the config requires n_img.
    n_pin, n_ig, n_fb, n_img = _targets(config)
    assert n_img > 1
    creator._llm = FakeLLM(make_content_response(n_pin, n_ig, n_fb, 1))
    sample_brief["id"] = _with_brief(db)

    items = creator.run(brief=sample_brief)
    imgs = [i for i in items if i["platform"] == "image"]
    assert len(imgs) == n_img                       # never fewer than configured
    assert all(i["body"].strip() for i in imgs)     # and never an empty prompt


def test_image_prompts_filled_deterministically_when_retry_fails(
    creator, config, db, sample_brief
):
    from onassis.llm import LLMError

    n_pin, n_ig, n_fb, n_img = _targets(config)

    class _ShortThenError:
        """Returns a short set first, then errors on the retry call."""

        def __init__(self, response):
            self.response = response
            self.calls = 0

        def generate_json(self, **_):
            self.calls += 1
            if self.calls == 1:
                return self.response
            raise LLMError("model unavailable")

    creator._llm = _ShortThenError(make_content_response(n_pin, n_ig, n_fb, 1))
    sample_brief["id"] = _with_brief(db)

    items = creator.run(brief=sample_brief)
    imgs = [i for i in items if i["platform"] == "image"]
    assert len(imgs) == n_img                       # deterministic fill guarantees the count
    assert all(i["body"].strip() for i in imgs)


def test_prompt_requests_configured_counts(creator, config, db, sample_brief):
    n_pin, n_ig, n_fb, n_img = _targets(config)
    creator._llm = FakeLLM(make_content_response(n_pin, n_ig, n_fb, n_img))
    sample_brief["id"] = _with_brief(db)
    creator.run(brief=sample_brief)

    prompt = creator._llm.last_prompt
    assert f"{n_pin} Pinterest posts" in prompt
    assert f"{n_ig} Instagram captions" in prompt
    assert f"{n_fb} Facebook posts" in prompt
    assert f"{n_img} cinematic image prompts" in prompt
    # the brief should inform the prompt
    assert sample_brief["theme"] in prompt


def test_off_target_counts_are_logged(creator, config, db, sample_brief, caplog):
    # Return one fewer Pinterest post than configured.
    n_pin, n_ig, n_fb, n_img = _targets(config)
    creator._llm = FakeLLM(make_content_response(n_pin - 1, n_ig, n_fb, n_img))
    sample_brief["id"] = _with_brief(db)

    with caplog.at_level(logging.WARNING):
        creator.run(brief=sample_brief)
    assert any("pinterest_posts" in r.getMessage() for r in caplog.records)


def test_content_schema_is_strict():
    schema = _content_schema()
    assert schema["additionalProperties"] is False
    for key in ("pinterest_posts", "instagram_captions", "facebook_posts", "image_prompts"):
        assert key in schema["required"]
