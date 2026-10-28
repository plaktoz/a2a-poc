import pytest

from board import Board, Card, UnknownCard, UnknownColumn, WipLimitExceeded


def test_add_card_places_new_card_in_first_column():
    board = Board(columns=["Todo", "Doing", "Done"])

    card = board.add_card("Write documentation")

    assert card == Card(id=1, title="Write documentation")
    assert board.cards("Todo") == [Card(id=1, title="Write documentation")]
    assert board.cards("Doing") == []
    assert board.cards("Done") == []


def test_cards_preserves_addition_order_for_multiple_titles():
    board = Board(columns=["Todo"])
    board.add_card("Zebra")
    board.add_card("Alpha")
    board.add_card("Middle")

    assert board.cards("Todo") == [
        Card(id=1, title="Zebra"),
        Card(id=2, title="Alpha"),
        Card(id=3, title="Middle"),
    ]


def test_cards_preserves_arrival_order_in_destination_column():
    board = Board(columns=["Todo", "Doing"])
    board.add_card("First created")
    board.add_card("Second created")
    board.add_card("Stays in Todo")

    board.move_card(2, "Doing")
    board.move_card(1, "Doing")

    assert board.cards("Doing") == [
        Card(id=2, title="Second created"),
        Card(id=1, title="First created"),
    ]


def test_move_card_removes_it_from_source_and_places_it_in_target():
    board = Board(columns=["Todo", "Done"])
    board.add_card("Write documentation")

    board.move_card(1, "Done")

    assert board.cards("Todo") == []
    assert board.cards("Done") == [Card(id=1, title="Write documentation")]


def test_move_card_removes_from_original_column_and_places_in_target_column():
    board = Board(columns=["Todo", "Doing"])
    card = board.add_card("Write documentation")

    board.move_card(card.id, "Doing")

    assert board.cards("Todo") == []
    assert [card.title for card in board.cards("Doing")] == ["Write documentation"]
    assert len(board.cards("Doing")) == 1
    assert board.cards("Doing")[0].id == card.id


def test_move_card_rejects_missing_card_and_missing_column_independently():
    board = Board(columns=["Todo", "Done"])
    board.add_card("Write documentation")

    with pytest.raises(UnknownCard):
        board.move_card(2, "Done")

    with pytest.raises(UnknownColumn):
        board.move_card(1, "Missing")


def test_full_wip_column_rejects_move_and_preserves_original_column():
    board = Board(["Todo", "Doing"], wip_limits={"Doing": 1})
    board.add_card("First task")
    board.add_card("Second task")
    board.move_card(1, "Doing")

    with pytest.raises(WipLimitExceeded):
        board.move_card(2, "Doing")

    assert board.cards("Todo") == [Card(id=2, title="Second task")]
    assert board.cards("Doing") == [Card(id=1, title="First task")]
