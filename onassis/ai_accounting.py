"""AI cost accounting (Sprint 42.2).

Every AI request — LLM completion or image generation — is costed and recorded
so no AI spend is invisible. Instrumentation is deliberately unobtrusive:

* A **thread-local context** carries the current workflow ``stage`` and
  ``product_id``/``campaign_id`` so individual call sites don't each have to
  thread that through. The Daily Cycle pushes context around each stage.
* A process-wide **recorder** (set once with the Database) writes one
  ``ai_requests`` row per call. If no recorder is set (unit tests, offline),
  recording is a silent no-op — accounting never breaks a generation.
* A small, **config-overridable pricing table** converts tokens/images to USD.

Pure, deterministic, and provider-agnostic.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any

from onassis.logger import get_logger

log = get_logger(__name__)

# --- Pricing (USD). Overridable via config.ai_pricing; sensible defaults. ----
# LLM: dollars per 1M tokens (input, output). Image: dollars per image by quality.
_DEFAULT_LLM_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (15.0, 75.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku": (0.80, 4.0),
}
_DEFAULT_LLM_FALLBACK = (15.0, 75.0)           # unknown model → assume Opus-class
_DEFAULT_IMAGE_PRICING: dict[str, float] = {   # per generated image (1024²-ish)
    "gpt-image-1:low": 0.011,
    "gpt-image-1:medium": 0.042,
    "gpt-image-1:high": 0.167,
    "gpt-image-1": 0.167,                      # default quality = high
    "dall-e-3": 0.040,
}
_DEFAULT_IMAGE_FALLBACK = 0.167

# Cost-per-product business targets (Sprint 42.2, Obj 10).
COST_TARGETS = [
    ("excellent", 0.75), ("good", 1.25), ("acceptable", 2.00),
    ("investigate", 3.00),   # >2.00 investigate, >3.00 critical
]


def rate_cost(cost_per_product: float | None) -> str:
    """Rate an average AI cost/product against the business targets."""
    if cost_per_product is None:
        return "unknown"
    c = float(cost_per_product)
    if c < 0.75:
        return "excellent"
    if c < 1.25:
        return "good"
    if c < 2.00:
        return "acceptable"
    if c < 3.00:
        return "investigate"
    return "critical"


class _Pricing:
    def __init__(self, config: Any = None) -> None:
        cfg = (getattr(config, "ai_pricing", None) or {}) if config else {}
        self.llm = {**_DEFAULT_LLM_PRICING, **(cfg.get("llm") or {})}
        self.image = {**_DEFAULT_IMAGE_PRICING, **(cfg.get("image") or {})}

    def llm_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        rate = self.llm.get(model)
        if rate is None:  # try a prefix match, else fallback
            rate = next((v for k, v in self.llm.items() if model.startswith(k)),
                        _DEFAULT_LLM_FALLBACK)
        cin, cout = rate
        return round(input_tokens / 1e6 * cin + output_tokens / 1e6 * cout, 6)

    def image_cost(self, model: str, quality: str, images: int) -> float:
        key = f"{model}:{quality}" if quality else model
        rate = self.image.get(key) or self.image.get(model) or _DEFAULT_IMAGE_FALLBACK
        return round(rate * max(0, images), 6)


# --- Thread-local workflow context ------------------------------------------
_ctx = threading.local()


def _current() -> dict[str, Any]:
    return getattr(_ctx, "value", {}) or {}


@contextmanager
def cost_context(*, stage: str | None = None, product_id: str | None = None,
                 campaign_id: int | None = None):
    """Tag every AI call made within this block with a stage/product."""
    prev = getattr(_ctx, "value", None)
    merged = dict(prev or {})
    if stage is not None:
        merged["stage"] = stage
    if product_id is not None:
        merged["product_id"] = product_id
    if campaign_id is not None:
        merged["campaign_id"] = campaign_id
    _ctx.value = merged
    try:
        yield
    finally:
        _ctx.value = prev


# --- Process-wide recorder --------------------------------------------------
class AICostRecorder:
    def __init__(self, db: Any, config: Any = None) -> None:
        self.db = db
        self.pricing = _Pricing(config)

    def record_llm(self, *, provider: str, model: str, input_tokens: int,
                   output_tokens: int, duration_ms: int, ok: bool = True,
                   detail: str | None = None) -> float:
        cost = self.pricing.llm_cost(model, input_tokens, output_tokens) if ok else 0.0
        self._write(provider=provider, model=model, kind="llm",
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    images=0, duration_ms=duration_ms, cost=cost, ok=ok, detail=detail)
        return cost

    def record_image(self, *, provider: str, model: str, quality: str = "",
                     images: int = 1, duration_ms: int = 0, ok: bool = True,
                     detail: str | None = None) -> float:
        cost = self.pricing.image_cost(model, quality, images) if ok else 0.0
        self._write(provider=provider, model=model, kind="image", input_tokens=0,
                    output_tokens=0, images=images, duration_ms=duration_ms,
                    cost=cost, ok=ok, detail=detail)
        return cost

    def _write(self, **fields: Any) -> None:
        ctx = _current()
        try:
            self.db.insert_ai_request({
                "provider": fields["provider"], "model": fields["model"],
                "kind": fields["kind"], "stage": ctx.get("stage"),
                "product_id": ctx.get("product_id"), "campaign_id": ctx.get("campaign_id"),
                "input_tokens": fields["input_tokens"],
                "output_tokens": fields["output_tokens"], "images": fields["images"],
                "duration_ms": fields["duration_ms"], "cost_usd": fields["cost"],
                "ok": fields["ok"], "detail": fields.get("detail")})
        except Exception as exc:  # accounting must NEVER break a generation
            log.warning("AI cost recording failed (non-fatal): %s", exc)


_recorder: AICostRecorder | None = None


def set_recorder(db: Any, config: Any = None) -> AICostRecorder:
    """Install the process-wide recorder (called once at app/cycle start)."""
    global _recorder
    _recorder = AICostRecorder(db, config)
    return _recorder


def record_llm(**kw: Any) -> float:
    return _recorder.record_llm(**kw) if _recorder is not None else 0.0


def record_image(**kw: Any) -> float:
    return _recorder.record_image(**kw) if _recorder is not None else 0.0
