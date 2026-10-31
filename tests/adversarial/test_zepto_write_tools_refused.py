"""
Zepto's MCP answers price queries and places REAL orders over the same
session — its own README: "not a sandbox or demo environment. Any order
placed through the Zepto MCP will be processed as a real Zepto order."

So these are not unit tests of a helper. They are the adversarial suite for
the one adapter in this repo that is wired to something which can spend
Gourab's money, and they must all fail closed. Nothing here touches the
network: the point is that the refusal happens BEFORE dispatch, so it holds
even with a valid token and a reachable server.

The grant is NOT the primary defence, though this file used to say it was.
Measured against the live server on 2026-09-27, a token carrying exactly
scope=tools:read is still offered update_cart, create_order,
create_wallet_order, create_upi_reserve_pay_order and
create_online_payment_order by tools/list. Zepto's authorization server records
the scope; its MCP server does not enforce it on the tool surface. So the
allowlist below is the defence, not a backstop to one.
"""
from __future__ import annotations

import pytest

# Invented names — these prove the guard is a positive allowlist rather than a
# list of things someone thought of.
_WRITE_TOOL_NAMES = [
    "place_order", "add_to_cart", "update_cart", "remove_from_cart",
    "cart_manage", "checkout", "pay", "create_order", "delete_item",
]

# REAL names, read off a live tools/list on 2026-09-27 under a tools:read grant.
# This list exists because the invented one above missed most of them: the first
# write guard matched "create_order" literally and therefore sailed past
# create_wallet_order and create_upi_reserve_pay_order — two tools that spend
# money. Test against the surface that exists, not the one in the README.
_REAL_WRITE_TOOLS_OFFERED = [
    "update_cart", "view_cart", "create_order", "create_online_payment_order",
    "create_wallet_order", "create_upi_reserve_pay_order", "get_payment_methods",
    "check_payment_status", "add_saved_address", "update_drop_zone",
    "select_store", "zepto_shop", "update_user_name",
]


@pytest.mark.parametrize("tool", _WRITE_TOOL_NAMES)
def test_write_shaped_tools_are_refused_before_dispatch(isolated_data_dir, tool):
    """No token, no network, no server — the refusal is structural. If this
    ever needed a connection to fail, the check would be in the wrong place."""
    from grocery.adapters.zepto_mcp import WriteToolRefused, ZeptoMCPAdapter

    with pytest.raises(WriteToolRefused):
        ZeptoMCPAdapter()._call(tool, {})


@pytest.mark.parametrize("tool", _REAL_WRITE_TOOLS_OFFERED)
def test_every_write_tool_zepto_actually_offers_is_refused(isolated_data_dir, tool):
    """Each of these was on offer to a read-scoped token. Each must be refused
    before dispatch. zepto_shop is here because its own schema accepts
    intent="order", which makes a single tool call an ordering surface."""
    from grocery.adapters.zepto_mcp import WriteToolRefused, ZeptoMCPAdapter

    with pytest.raises(WriteToolRefused):
        ZeptoMCPAdapter()._call(tool, {})


def test_the_write_guard_has_no_exceptions(isolated_data_dir):
    """There was briefly one, for update_user_name, so that the adapter could
    complete Zepto's registration gate itself. It is gone: registration is a
    human action in Zepto's app. If an exemption mechanism comes back, it should
    have to delete this test to do it."""
    from grocery.adapters import zepto_mcp

    assert not hasattr(zepto_mcp, "_WRITE_SHAPED_EXEMPT")
    for name in zepto_mcp._ALLOWED_TOOLS:
        assert not zepto_mcp._is_write_shaped(name)


def test_allowlist_is_positive_not_a_denylist(isolated_data_dir):
    """A tool Zepto adds next month must be refused by DEFAULT. A denylist
    would admit it silently, which is the whole failure mode."""
    from grocery.adapters.zepto_mcp import WriteToolRefused, ZeptoMCPAdapter

    with pytest.raises(WriteToolRefused):
        ZeptoMCPAdapter()._call("some_tool_invented_next_month", {})


def test_only_read_tools_are_on_the_allowlist(isolated_data_dir):
    from grocery.adapters import zepto_mcp

    for name in zepto_mcp._ALLOWED_TOOLS:
        assert not any(marker in name for marker in zepto_mcp._WRITE_SHAPED), (
            f"{name!r} is on the read allowlist but reads as a write tool"
        )


def test_requests_only_the_read_scope(isolated_data_dir):
    from grocery.adapters import zepto_mcp

    assert zepto_mcp.REQUIRED_SCOPE == "tools:read"


@pytest.mark.parametrize("method,args", [
    ("build_cart", ([],)),
    ("place_order", ("cart_1", "pay_1")),
])
def test_contract_write_methods_raise_even_when_authorized(isolated_data_dir, method, args):
    """authorized=True is the broker's signal elsewhere in the system. This
    adapter must ignore it: nothing has ever wired it to something that
    could check that flag for real."""
    from grocery.adapters.zepto_mcp import ZeptoMCPAdapter

    with pytest.raises(NotImplementedError):
        getattr(ZeptoMCPAdapter(), method)(*args, authorized=True)


def test_a_grant_with_write_scope_is_refused_at_rest(isolated_data_dir):
    """Defence in depth on the grant itself: if the authorization server ever
    hands back more than we asked for, we refuse to persist it rather than
    hold write authority we did not request."""
    from grocery.mcp import oauth

    broad = oauth.StoredToken(access_token="x", scope="tools:read tools:write")
    with pytest.raises(oauth.ScopeTooBroad):
        oauth.store("test_token", broad, allowed_scopes={"tools:read"})


def test_a_grant_with_no_scope_at_all_is_refused(isolated_data_dir):
    """An empty scope is an absence of evidence, not evidence of narrowness.
    Same rule as an unsourced price."""
    from grocery.mcp import oauth

    with pytest.raises(oauth.ScopeTooBroad):
        oauth.store("test_token", oauth.StoredToken(access_token="x", scope=""),
                    allowed_scopes={"tools:read"})


def test_a_read_only_grant_is_stored_and_never_logs_the_token(isolated_data_dir):
    from grocery.mcp import oauth

    token = oauth.StoredToken(access_token="super-secret", scope="tools:read",
                              refresh_token="r", expires_at=0.0)
    oauth.store("test_token", token, allowed_scopes={"tools:read"})
    assert oauth.load("test_token").access_token == "super-secret"
    # redacted() is what /health and the Control page render.
    assert "super-secret" not in str(token.redacted())


def test_search_without_a_token_asks_a_human_rather_than_authorizing_itself(isolated_data_dir):
    """The adapter must never start an OAuth flow on its own — linking a real
    Zepto account is a decision taken at a keyboard, not by an agent."""
    from grocery.adapters.zepto_mcp import ZeptoMCPAdapter, ZeptoNotConnected

    with pytest.raises(ZeptoNotConnected, match="--connect"):
        ZeptoMCPAdapter().search("milk")
