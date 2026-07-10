from typing import Dict

from .models import PositionCurve


class PositionManager:
    def __init__(self, positions: Dict[str, PositionCurve]):
        self._positions = dict(positions)

    def get(self, market_id: str, title: str) -> PositionCurve:
        return self._positions.get(market_id, PositionCurve(market_id=market_id, title=title))

    def total_exposure(self) -> float:
        return sum(position.total_cost for position in self._positions.values())
