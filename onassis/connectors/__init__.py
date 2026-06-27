"""Revenue connectors.

Marketplaces and platforms (Etsy, Gelato, Pinterest, advertising networks)
plug into the Revenue Engine by implementing
:class:`~onassis.connectors.base.RevenueConnector` — they emit canonical order
dicts and the engine ingests them unchanged. None of them require any change to
the core revenue/decision engine.
"""

from onassis.connectors.base import ManualConnector, RevenueConnector

__all__ = ["RevenueConnector", "ManualConnector"]
