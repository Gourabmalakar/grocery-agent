import pytest


def test_none_by_default(isolated_data_dir):
    from grocery import killswitch
    assert killswitch.state().tier == killswitch.Tier.NONE


def test_panic_blocks_everything(isolated_data_dir):
    from grocery import killswitch
    killswitch.trip_panic(reason="test", set_by="tester")
    assert killswitch.state().tier == killswitch.Tier.PANIC
    with pytest.raises(killswitch.KillSwitchEngaged):
        killswitch.require_not_killed("anything")


def test_pause_blocks_new_work(isolated_data_dir):
    from grocery import killswitch
    killswitch.trip_pause(reason="test", set_by="tester")
    assert killswitch.state().tier == killswitch.Tier.PAUSE
    with pytest.raises(killswitch.KillSwitchEngaged):
        killswitch.require_not_killed("create_order")


def test_clear_panic_requires_explicit_call_with_reason(isolated_data_dir):
    from grocery import killswitch
    killswitch.trip_panic(reason="test", set_by="tester")
    killswitch.clear_panic(cleared_by="human", typed_reason="false alarm, verified")
    assert killswitch.state().tier == killswitch.Tier.NONE


def test_fails_closed_on_unreadable_state(isolated_data_dir, monkeypatch):
    from grocery import killswitch, config
    def boom(self):
        raise OSError("disk unreadable")
    monkeypatch.setattr(type(config.PANIC_FILE), "exists", boom)
    s = killswitch.state()
    assert s.tier == killswitch.Tier.PANIC
    assert "unreadable" in s.reason


def test_order_operations_refuse_when_panicked(isolated_data_dir):
    from grocery import killswitch
    from grocery.mandates import intent as intent_m
    from console import approval
    killswitch.trip_panic(reason="test", set_by="tester")
    signed_intent = intent_m.new_intent(items=["milk"], max_total_inr=500)
    with pytest.raises(killswitch.KillSwitchEngaged):
        approval.create_order(signed_intent)


def test_burn_deletes_vault_and_writes_checklist(isolated_data_dir):
    from grocery import killswitch, config
    (config.VAULT_DIR / "some_credential.enc").write_text("fake-encrypted-blob")
    checklist = killswitch.burn(executed_by="tester")
    assert "BURN executed" in checklist
    assert list(config.VAULT_DIR.glob("*")) == []
    assert killswitch.state().tier == killswitch.Tier.PANIC  # BURN also trips PANIC
