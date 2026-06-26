"""Tests for the Anthropic LLM wrapper (the SDK client is mocked)."""

from __future__ import annotations

import pytest

import onassis.llm as llmmod
from onassis.llm import LLMClient, LLMError


class _Block:
    def __init__(self, type_: str, text: str | None = None) -> None:
        self.type = type_
        self.text = text


class _Message:
    def __init__(self, content: list[_Block]) -> None:
        self.content = content


class _Stream:
    def __init__(self, message: _Message) -> None:
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self) -> _Message:
        return self._message


class _Messages:
    def __init__(self, message: _Message) -> None:
        self._message = message
        self.kwargs: dict | None = None

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return _Stream(self._message)


class _FakeClient:
    def __init__(self, message: _Message) -> None:
        self.messages = _Messages(message)


def _patch_client(monkeypatch, message: _Message) -> dict:
    """Patch anthropic.Anthropic to return a fake client; expose it to the test."""
    created: dict = {}

    def factory(api_key=None):
        client = _FakeClient(message)
        created["client"] = client
        return client

    monkeypatch.setattr(llmmod.anthropic, "Anthropic", factory)
    return created


def test_missing_api_key_raises(config):
    config.anthropic_api_key = None
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        LLMClient(config)


def test_generate_json_parses_text_block(config, monkeypatch):
    message = _Message([_Block("thinking"), _Block("text", '{"campaign_name": "X"}')])
    created = _patch_client(monkeypatch, message)

    client = LLMClient(config)
    result = client.generate_json(system="sys", prompt="hi", schema={"type": "object"})

    assert result == {"campaign_name": "X"}
    # The request should be configured the way we expect.
    kwargs = created["client"].messages.kwargs
    assert kwargs["model"] == config.llm_model
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"]["effort"] == config.llm_effort
    assert kwargs["output_config"]["format"]["type"] == "json_schema"


def test_generate_json_invalid_json_raises(config, monkeypatch):
    _patch_client(monkeypatch, _Message([_Block("text", "{not valid json")]))
    client = LLMClient(config)
    with pytest.raises(LLMError, match="invalid JSON"):
        client.generate_json(system="s", prompt="p", schema={})


def test_generate_json_no_text_block_raises(config, monkeypatch):
    _patch_client(monkeypatch, _Message([_Block("thinking")]))
    client = LLMClient(config)
    with pytest.raises(LLMError, match="no text content"):
        client.generate_json(system="s", prompt="p", schema={})
