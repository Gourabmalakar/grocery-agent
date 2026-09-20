"""
Zepto's MCP answers price queries and places REAL orders over the same
session — its own README: "not a sandbox or demo environment. Any order
placed through the Zepto MCP will be processed as a real Zepto order."

So these are not unit tests of a helper. They are the adversarial suite for
the one adapter in this repo that is wired to something which can spend
Gourab's money, and they must all fail closed. Nothing here touches the
network: the point is that the refusal happens BEFORE dispatch, so it holds
even with a valid token and a reachable server.

The grant is the primary defence (scope=tools:read, enforced by Zepto's own
authorization server). These cover the second and third layers.
"""
from __future__ import annotations

import pytest

_WRITE_TOOL_NAMES = [
    "place_order", "add_to_cart", "update_cart", "remove_from_cart",
    "cart_manage", "checkout", "pay", "create_order", "delete_item",
]


@pytest.mark.parametrize("tool", _WRITE_TOOL_NAMES)
def test_write_shaped_tools_are_refused_before_dispatch(isolated_data_dir, tool):
    """No token, no network, no server — the refusal is structural. If this
    ever needed a connection to fail, the check would be in the wrong place."""
    from grocery.adapters.zepto_mcp import WriteToolRefused, ZeptoMCPAdapter

    with pytest.raises(WriteToolRefused):
        ZeptoMCPAdapter()._call(tool, {})


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
