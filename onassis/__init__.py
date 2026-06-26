"""ONASSIS — an autonomous content engine for lifestyle brands.

This is Version 0.1: a deliberately small, well-structured foundation.
The only end-to-end feature is the daily content pipeline:

    Content Director  ->  builds a content brief
    Content Creator   ->  turns the brief into platform-ready content
    (Publisher / Analytics are placeholders for future versions.)

Everything is persisted to SQLite. The architecture is intentionally
modular so each piece can be swapped or extended independently.
"""

__version__ = "0.1.0"
