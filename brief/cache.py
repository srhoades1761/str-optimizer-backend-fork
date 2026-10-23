"""Dict-style AirROI cache backed by Postgres (services/db.py market_cache table)."""
from __future__ import annotations

import json
from dataclasses import asdict

from .market import MarketStats, Month


class DbMarketCache:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _key(key) -> str:
        return json.dumps(list(key))

    def get(self, key):
        d = self.db.cache_get(self._key(key))
        if d is None:
            return None
        return MarketStats(**{**d, "months": [Month(**m) for m in d["months"]]})

    def __contains__(self, key):
        return self.get(key) is not None

    def __getitem__(self, key):
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v

    def __setitem__(self, key, value: MarketStats):
        self.db.cache_set(self._key(key), asdict(value))
