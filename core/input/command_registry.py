# -*- coding: utf-8 -*-
"""
CommandRegistry — maps command strings and aliases to tool factory keys.

Usage:
    registry.register("LINE", "line", aliases=["L"])
    key = registry.resolve("L")   # → "line"
"""


class CommandRegistry:
    def __init__(self):
        self._map: dict[str, str] = {}   # UPPER alias → tool_key

    def register(self, tool_key: str, *aliases):
        """Register a tool_key with one or more command aliases (case-insensitive)."""
        for alias in aliases:
            self._map[alias.upper()] = tool_key

    def resolve(self, token: str) -> str | None:
        """Return the tool_key for the given command token, or None."""
        return self._map.get(token.strip().upper())

    def all_aliases(self) -> list:
        return sorted(self._map.keys())
