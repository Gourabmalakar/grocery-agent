from grocery import narrator


def _cart(merchant="quickbasket", total=130.0):
    return {
        "merchant": merchant,
        "landed_total_inr": total,
        "fees": {"delivery": 15, "handling": 8},
        "line_items": [
            {"name": "Farm Eggs, Pack of 6", "brand": "Generic", "qty": 5, "line_total_inr": 225.0},
            {"name": "Aashirvaad Whole Wheat Atta 1kg", "brand": "Aashirvaad", "qty": 1, "line_total_inr": 62.0},
        ],
    }


def test_narrate_proposal_mentions_every_item_and_total():
    msg = narrator.narrate_proposal(
        cart=_cart(), comparison=[{"merchant": "quickbasket", "landed_total_inr": 130.0}],
        missing_items=[], not_found_items=[], max_total_inr=800,
    )
    assert "Farm Eggs" in msg
    assert "Aashirvaad" in msg
    assert "130.00" in msg
    assert "800" in msg


def test_narrate_proposal_names_the_cheaper_alternative_when_others_exist():
    comparison = [
        {"merchant": "quickbasket", "landed_total_inr": 130.0},
        {"merchant": "freshmart", "landed_total_inr": 460.0},
    ]
    msg = narrator.narrate_proposal(cart=_cart(), comparison=comparison,
                                     missing_items=[], not_found_items=[], max_total_inr=800)
    assert "freshmart" in msg
    assert "460.00" in msg


def test_narrate_proposal_flags_not_found_items_honestly():
    msg = narrator.narrate_proposal(
        cart=_cart(), comparison=[{"merchant": "quickbasket", "landed_total_inr": 130.0}],
        missing_items=[], not_found_items=["1 pack of bread", "3 kg onions"], max_total_inr=800,
    )
    assert "1 pack of bread" in msg
    assert "3 kg onions" in msg


def test_narrate_proposal_revision_opening_differs_from_fresh():
    fresh = narrator.narrate_proposal(cart=_cart(), comparison=[], missing_items=[],
                                       not_found_items=[], max_total_inr=800)
    revised = narrator.narrate_proposal(cart=_cart(), comparison=[], missing_items=[],
                                         not_found_items=[], max_total_inr=800,
                                         revision_instruction="Amul not Britannia")
    assert fresh.startswith("Hi!")
    assert revised.startswith("Done")
    assert "Amul not Britannia" in revised


def test_narrate_revision_not_understood_quotes_the_instruction():
    msg = narrator.narrate_revision_not_understood(instruction_text="pilsbury multigrain please")
    assert "pilsbury multigrain please" in msg


def test_narrate_for_order_dispatches_by_state():
    assert "moment" in narrator.narrate_for_order({"state": "draft"})
    assert narrator.narrate_for_order({"state": "proposed", "narrative": "custom text"}) == "custom text"
    assert "pay" in narrator.narrate_for_order({"state": "approved", "cart": _cart()}).lower()
    assert "cancel" in narrator.narrate_for_order({"state": "aborted"}).lower()

    done_msg = narrator.narrate_for_order({
        "state": "done", "cart": _cart(), "payment": {"amount_inr": 130.0}, "merchant_order_id": "mockorder_abc",
    })
    assert "mockorder_abc" in done_msg
    assert "130.00" in done_msg
