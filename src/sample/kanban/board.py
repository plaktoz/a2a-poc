from __future__ import annotations

from dataclasses import dataclass


class UnknownCard(Exception):
    pass


class UnknownColumn(Exception):
    pass


class WipLimitExceeded(Exception):
    pass


@dataclass(frozen=True)
class Card:
    id: int
    title: str


class Board:
    def __init__(self, columns: list[str], wip_limits: dict[str, int] | None = None) -> None:
        self._columns: dict[str, list[Card]] = {name: [] for name in columns}
        self._first_column = columns[0]
        self._wip_limits = wip_limits or {}
        self._next_id = 1

    def add_card(self, title: str) -> Card:
        card = Card(id=self._next_id, title=title)
        self._next_id += 1
        self._columns[self._first_column].append(card)
        return card

    def cards(self, column: str) -> list[Card]:
        return list(self._columns[column])

    def move_card(self, card_id: int, to_column: str) -> None:
        if to_column not in self._columns:
            raise UnknownColumn(to_column)
        for cards in self._columns.values():
            for card in cards:
                if card.id == card_id:
                    limit = self._wip_limits.get(to_column)
                    if limit is not None and len(self._columns[to_column]) >= limit:
                        raise WipLimitExceeded(to_column)
                    cards.remove(card)
                    self._columns[to_column].append(card)
                    return
        raise UnknownCard(card_id)
