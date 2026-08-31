"""
test_cards.py — Tests for card selection / dashboard card data.

Covers:
  - AVAILABLE_CARDS has 20 entries
  - All 20 card IDs are unique
  - Card descriptions exist for all entries
"""
from __future__ import annotations

import pytest

from main import AVAILABLE_CARDS


class TestAvailableCards:
    """Tests for the AVAILABLE_CARDS constant."""

    def test_available_cards_has_20_entries(self):
        """AVAILABLE_CARDS must contain exactly 20 cards."""
        assert len(AVAILABLE_CARDS) == 20, (
            f"Expected 20 cards, got {len(AVAILABLE_CARDS)}"
        )

    def test_all_card_ids_are_unique(self):
        """No two cards may share the same id."""
        ids = [c["id"] for c in AVAILABLE_CARDS]
        assert len(ids) == len(set(ids)), (
            f"Duplicate card IDs found: {ids}"
        )

    def test_all_cards_have_id_field(self):
        for card in AVAILABLE_CARDS:
            assert "id" in card, f"Card missing 'id' field: {card}"
            assert card["id"], f"Card has empty 'id': {card}"

    def test_all_cards_have_name_field(self):
        for card in AVAILABLE_CARDS:
            assert "name" in card, f"Card missing 'name' field: {card}"
            assert card["name"], f"Card has empty 'name': {card}"

    def test_all_cards_have_description(self):
        """Every card must have a non-empty description."""
        for card in AVAILABLE_CARDS:
            assert "description" in card, (
                f"Card {card.get('id', '?')} missing 'description' field"
            )
            assert card["description"], (
                f"Card {card['id']} has empty description"
            )

    def test_card_ids_are_strings(self):
        for card in AVAILABLE_CARDS:
            assert isinstance(card["id"], str), (
                f"Card id {card['id']} is not a string"
            )

    def test_card_descriptions_are_strings(self):
        for card in AVAILABLE_CARDS:
            assert isinstance(card["description"], str), (
                f"Card {card['id']} description is not a string"
            )

    def test_card_names_are_strings(self):
        for card in AVAILABLE_CARDS:
            assert isinstance(card["name"], str), (
                f"Card {card['id']} name is not a string"
            )

    def test_card_ids_are_lowercase_snake_case(self):
        """Card IDs should follow a consistent naming convention."""
        for card in AVAILABLE_CARDS:
            cid = card["id"]
            # Should be lowercase, no spaces, only alphanumeric + underscore
            assert cid == cid.lower(), f"Card id {cid!r} is not lowercase"
            assert " " not in cid, f"Card id {cid!r} contains a space"
            assert cid.replace("_", "").isalnum(), (
                f"Card id {cid!r} contains non-alphanumeric characters besides underscore"
            )

    def test_expected_card_ids_present(self):
        """Sanity check: known core cards should be present."""
        known_ids = {"sales", "reviews", "checklist", "goals", "stress", "invoices"}
        actual_ids = {c["id"] for c in AVAILABLE_CARDS}
        missing = known_ids - actual_ids
        assert not missing, f"Missing expected card IDs: {missing}"

    def test_descriptions_are_meaningful(self):
        """Descriptions should be more than a single word."""
        for card in AVAILABLE_CARDS:
            desc = card["description"]
            word_count = len(desc.split())
            assert word_count >= 2, (
                f"Card {card['id']} description too short ({word_count} words): {desc!r}"
            )
