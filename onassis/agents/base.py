"""The agent framework foundation.

Every ONASSIS agent inherits from :class:`BaseAgent`. The base class
gives each agent three things for free:

    * a reference to shared :class:`~onassis.config.Config`
    * a reference to the shared :class:`~onassis.database.Database`
    * a namespaced logger and a uniform ``execute`` wrapper that logs
      start/finish/failure consistently

Subclasses implement a single method, :meth:`run`. Callers should invoke
:meth:`execute` (not ``run`` directly) so they get the logging and error
handling for free. This keeps every agent's lifecycle identical, which is
what makes the system easy to extend.
"""

from __future__ import annotations

import abc
from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger


class BaseAgent(abc.ABC):
    """Abstract base class for all agents."""

    #: Human-readable agent name; defaults to the class name.
    name: str = "BaseAgent"

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.log = get_logger(f"onassis.agent.{self.name}")

    @abc.abstractmethod
    def run(self, **kwargs: Any) -> Any:
        """Perform the agent's work. Implemented by every subclass."""
        raise NotImplementedError

    def execute(self, **kwargs: Any) -> Any:
        """Run the agent with uniform logging and error propagation."""
        self.log.info("[%s] starting", self.name)
        try:
            result = self.run(**kwargs)
        except Exception:
            self.log.exception("[%s] failed", self.name)
            raise
        self.log.info("[%s] finished", self.name)
        return result

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} name={self.name!r}>"
