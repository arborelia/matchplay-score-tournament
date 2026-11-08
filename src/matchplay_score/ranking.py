__package__ = "matchplay_score"
from .matchplay_api import get_cards, get_tournament_data, get_tournament_data_cached
from .tournament import (
    Card,
    creation_order,
    parse_card,
    parse_tournament,
)


def best_cards(
    tournament_id: int, use_cache: bool = True, *, include_invalid: bool = False
) -> list[Card]:
    if use_cache:
        tournament_data = get_tournament_data_cached(tournament_id)
    else:
        tournament_data = get_tournament_data(tournament_id)
    card_data: list[dict] = get_cards(tournament_id)

    tournament = parse_tournament(tournament_data)

    cards_by_player: dict[str, Card] = {}
    excluded_by_player: dict[str, Card] = {}
    for card_item in card_data:
        card: Card = parse_card(card_item, tournament)
        if card.is_valid():
            if card.player_name not in cards_by_player or (
                cards_by_player[card.player_name].computed_score()
                < card.computed_score()
            ):
                cards_by_player[card.player_name] = card
        elif include_invalid:
            previous = excluded_by_player.get(card.player_name)
            if previous is None or creation_order(
                card.created_at, card.card_id
            ) > creation_order(previous.created_at, previous.card_id):
                excluded_by_player[card.player_name] = card

    ranked = sorted(cards_by_player.values(), key=Card.computed_score, reverse=True)
    unranked = [
        card for name, card in excluded_by_player.items() if name not in cards_by_player
    ]
    return ranked + sorted(unranked, key=lambda card: card.player_name.casefold())


def player_cards(
    tournament_id: int, player_name: str, use_cache: bool = True
) -> list[Card]:
    """Include invalid cards in player history so their exclusion is visible."""
    tournament_data = (
        get_tournament_data_cached(tournament_id)
        if use_cache
        else get_tournament_data(tournament_id)
    )
    tournament = parse_tournament(tournament_data)
    if player_name not in tournament.players_by_id.values() and "_" in player_name:
        player_name = player_name.replace("_", " ")
    cards = [parse_card(item, tournament) for item in get_cards(tournament_id)]
    matching = [card for card in cards if card.player_name == player_name]
    dated = [card for card in matching if card.created_at or card.card_id]
    if dated:
        newest = max(
            dated, key=lambda card: creation_order(card.created_at, card.card_id)
        )
        newest.is_latest = True
    return sorted(
        matching,
        key=lambda card: creation_order(card.created_at, card.card_id),
        reverse=True,
    )


def main():
    from pprint import pprint

    pprint(best_cards(281672))


if __name__ == "__main__":
    main()
