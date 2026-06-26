"""Tests for the agent framework foundation (BaseAgent)."""

from __future__ import annotations

import logging

import pytest

from onassis.agents.base import BaseAgent


class _OkAgent(BaseAgent):
    name = "OkAgent"

    def run(self, **kwargs):
        return {"echo": kwargs}


class _BoomAgent(BaseAgent):
    name = "BoomAgent"

    def run(self, **kwargs):
        raise ValueError("boom")


def test_execute_returns_run_result(config, db):
    agent = _OkAgent(config, db)
    result = agent.execute(x=1, y=2)
    assert result == {"echo": {"x": 1, "y": 2}}


def test_execute_logs_start_and_finish(config, db, caplog):
    agent = _OkAgent(config, db)
    with caplog.at_level(logging.INFO):
        agent.execute()
    messages = [r.getMessage() for r in caplog.records]
    assert any("starting" in m for m in messages)
    assert any("finished" in m for m in messages)


def test_execute_propagates_and_logs_errors(config, db, caplog):
    agent = _BoomAgent(config, db)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValueError, match="boom"):
            agent.execute()
    assert any("failed" in r.getMessage() for r in caplog.records)


def test_base_agent_is_abstract(config, db):
    with pytest.raises(TypeError):
        BaseAgent(config, db)  # type: ignore[abstract]


def test_agent_has_config_db_and_logger(config, db):
    agent = _OkAgent(config, db)
    assert agent.config is config
    assert agent.db is db
    assert agent.log.name == "onassis.agent.OkAgent"
