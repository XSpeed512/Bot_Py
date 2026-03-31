"""
news/news_filter.py
===================
Economic calendar integration (placeholder).

This module provides a clean interface that the main bot loop uses to
decide whether trading should be paused around high-impact news events.

Current state
-------------
``check_news()`` is a stub that always returns ``True`` (trading allowed).

Future integration
------------------
Replace the body of ``check_news()`` with a call to an economic
calendar API (e.g. ForexFactory JSON feed, Trading Economics,
Investing.com, or a paid data vendor).

The ``config`` module exposes:
    NEWS_FILTER_ENABLED          – Master on/off switch.
    NEWS_FILTER_MINUTES_BEFORE   – Pause window before event.
    NEWS_FILTER_MINUTES_AFTER    – Pause window after event.
    ECONOMIC_CALENDAR_API_KEY    – API key for the calendar service.

Suggested structure for the real implementation:
    1. Fetch upcoming high-impact events for the symbols being traded.
    2. Check if ``now`` falls within the pause window of any event.
    3. Return False (block trading) if inside a window, True otherwise.
"""

from __future__ import annotations

from datetime import datetime

import config
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes (used when real API is integrated)
# ---------------------------------------------------------------------------

class NewsEvent:
    """
    Lightweight container for a single economic calendar event.

    Attributes
    ----------
    time    : datetime  Scheduled release time (UTC).
    currency: str       Affected currency (e.g. "USD").
    impact  : str       ``"HIGH"`` | ``"MEDIUM"`` | ``"LOW"``
    title   : str       Human-readable event name.
    """

    def __init__(self, time: datetime, currency: str, impact: str, title: str) -> None:
        self.time     = time
        self.currency = currency
        self.impact   = impact
        self.title    = title

    def __repr__(self) -> str:
        return (
            f"NewsEvent({self.time.strftime('%Y-%m-%d %H:%M')} UTC | "
            f"{self.impact} | {self.currency} | {self.title})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_news(symbol: str = "") -> bool:
    """
    Determine whether trading is permitted right now.

    Parameters
    ----------
    symbol : str, optional
        The instrument about to be traded.  When the real API is
        integrated this allows filtering events by relevant currency.

    Returns
    -------
    bool
        ``True``  – Trading is safe to proceed.
        ``False`` – A high-impact news event is imminent or just passed;
                    trading should be paused.

    Notes
    -----
    Currently returns ``True`` unconditionally.
    Replace the body with a real calendar lookup before going live.
    """
    if not config.NEWS_FILTER_ENABLED:
        return True   # Filter disabled globally

    # ------------------------------------------------------------------
    # TODO: Implement real calendar lookup here.
    #
    # Example skeleton:
    #
    #   events = _fetch_high_impact_events()
    #   now    = datetime.utcnow()
    #   before = timedelta(minutes=config.NEWS_FILTER_MINUTES_BEFORE)
    #   after  = timedelta(minutes=config.NEWS_FILTER_MINUTES_AFTER)
    #
    #   for event in events:
    #       if event.time - before <= now <= event.time + after:
    #           logger.info("NEWS FILTER ACTIVE: %s", event)
    #           return False
    #
    # ------------------------------------------------------------------

    return True   # Placeholder: always allow trading


def _fetch_high_impact_events() -> list[NewsEvent]:
    """
    Placeholder for future economic calendar API call.

    Returns an empty list until the real integration is built.
    """
    # Future implementation:
    #   url = f"https://api.example.com/calendar?apikey={config.ECONOMIC_CALENDAR_API_KEY}"
    #   response = requests.get(url)
    #   data = response.json()
    #   return [NewsEvent(...) for item in data if item["impact"] == "HIGH"]

    return []
