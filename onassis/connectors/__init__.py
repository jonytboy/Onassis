"""Revenue connectors.

Marketplaces and platforms (Etsy, Gelato, Pinterest, advertising networks)
plug into the Revenue Engine by implementing
:class:`~onassis.connectors.base.RevenueConnector` — they emit canonical order
dicts and the engine ingests them unchanged. None of them require any change to
the core revenue/decision engine.
"""

from onassis.connectors.base import ManualConnector, RevenueConnector
from onassis.connectors.etsy import EtsyConnector
from onassis.connectors.etsy_client import EtsyClient, EtsyConfigError
from onassis.connectors.etsy_oauth import (
    EtsyAuthError,
    EtsyOAuth,
    TokenStore,
    build_etsy_oauth,
)

__all__ = [
    "RevenueConnector",
    "ManualConnector",
    "EtsyConnector",
    "EtsyClient",
    "EtsyConfigError",
    "EtsyOAuth",
    "EtsyAuthError",
    "TokenStore",
    "build_etsy_oauth",
]
