"""Best-effort usage accounting from normalized provider events (#54)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from symphony.events import AgentEvent


@dataclass(slots=True)
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    events_count: int = 0

    @property
    def has_usage(self) -> bool:
        return self.events_count > 0

    def apply_event(self, event: AgentEvent) -> bool:
        usage = extract_usage(event)
        if usage is None:
            return False
        self.input_tokens += _int(usage.get("input_tokens"))
        self.output_tokens += _int(usage.get("output_tokens"))
        self.cache_creation_input_tokens += _int(
            usage.get("cache_creation_input_tokens")
        )
        self.cache_read_input_tokens += _int(usage.get("cache_read_input_tokens"))
        supplied_total = _int(usage.get("total_tokens"))
        if supplied_total:
            self.total_tokens += supplied_total
        else:
            self.total_tokens += (
                _int(usage.get("input_tokens"))
                + _int(usage.get("output_tokens"))
                + _int(usage.get("cache_creation_input_tokens"))
                + _int(usage.get("cache_read_input_tokens"))
            )
        cost = _float(usage.get("cost_usd"))
        if cost is not None:
            self.cost_usd = (self.cost_usd or 0.0) + cost
        self.events_count += 1
        return True

    def to_json(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "events_count": self.events_count,
        }


def extract_usage(event: AgentEvent) -> dict[str, Any] | None:
    """Extract normalized usage from an event, ignoring malformed payloads."""

    payload = event.payload or {}
    if event.event == "usage":
        candidate = payload.get("usage", payload)
    else:
        candidate = payload.get("usage")
    if not isinstance(candidate, dict):
        return None
    if not any(_is_usage_key(key) for key in candidate):
        return None
    return candidate


def _is_usage_key(key: object) -> bool:
    return key in {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "total_tokens",
        "cost_usd",
    }


def _int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            return max(0.0, float(value))
        except ValueError:
            return None
    return None


__all__ = ["UsageTotals", "extract_usage"]
