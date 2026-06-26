"""ONASSIS agents.

Each agent is a self-contained unit of work that subclasses
:class:`onassis.agents.base.BaseAgent`. v0.1 ships four:

    ContentDirector  — builds the daily content brief        (working)
    ContentCreator   — turns a brief into platform content    (working)
    Publisher        — pushes content to social platforms     (placeholder)
    AnalyticsAgent   — reports on performance                 (placeholder)

New agents only need to subclass BaseAgent and implement ``run``.
"""

from onassis.agents.analytics import AnalyticsAgent
from onassis.agents.base import BaseAgent
from onassis.agents.content_creator import ContentCreator
from onassis.agents.content_director import ContentDirector
from onassis.agents.publisher import Publisher

__all__ = [
    "BaseAgent",
    "ContentDirector",
    "ContentCreator",
    "Publisher",
    "AnalyticsAgent",
]
