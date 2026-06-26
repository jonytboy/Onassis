"""Anthropic LLM client wrapper.

A single, small abstraction the agents use to turn a prompt into validated
JSON. Keeping all Anthropic-specific code here means the agents stay focused
on *what* to ask for, not *how* to call the API — and a future provider swap
is a change to this one file.

Design notes
------------
* Uses the official ``anthropic`` SDK and the configured model
  (default ``claude-opus-4-8``).
* Uses **structured outputs** (``output_config.format``) so the model is
  constrained to a JSON schema — no brittle prompt-and-regex parsing.
* Uses **adaptive thinking** for higher-quality, more considered output.
* Streams the response and calls ``get_final_message()`` so large
  generations never hit an HTTP idle timeout.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from onassis.config import Config
from onassis.logger import get_logger

log = get_logger(__name__)


class LLMError(RuntimeError):
    """Raised when the LLM call fails or returns unusable output."""


class LLMClient:
    """Thin wrapper around the Anthropic Messages API for JSON generation."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.model = config.llm_model
        self.effort = config.llm_effort
        self.max_tokens = config.llm_max_tokens

        # The SDK reads ANTHROPIC_API_KEY from the environment automatically
        # (config loads .env first). Fail early with a clear message if it's
        # missing rather than deep inside an API call.
        if not config.anthropic_api_key:
            raise LLMError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add "
                "your key, or export ANTHROPIC_API_KEY before running."
            )
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate a JSON object validated against ``schema``.

        Args:
            system: System prompt — sets the model's role and voice.
            prompt: The user instruction.
            schema: A JSON Schema the response is constrained to.

        Returns:
            The parsed JSON object.

        Raises:
            LLMError: on API failure or if no JSON text comes back.
        """
        try:
            with self._client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                message = stream.get_final_message()
        except anthropic.APIError as exc:  # network, auth, rate limit, etc.
            raise LLMError(f"Anthropic API call failed: {exc}") from exc

        # With output_config.format the model returns its JSON in a text block.
        text = next((b.text for b in message.content if b.type == "text"), None)
        if not text:
            raise LLMError("LLM returned no text content to parse.")

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM returned invalid JSON: {exc}") from exc
