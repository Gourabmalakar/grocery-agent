def test_audit_chain_appends_and_verifies(isolated_data_dir):
    from grocery.audit import log as audit_log, verify
    audit_log.append("test.event", {"a": 1})
    audit_log.append("test.event", {"a": 2})
    ok, msg = verify.verify_audit_chain()
    assert ok, msg


def test_audit_chain_detects_tamper(isolated_data_dir, tmp_path):
    from grocery.audit import log as audit_log, verify
    import json
    audit_log.append("test.event", {"a": 1})
    # Tamper with the file directly, bypassing append().
    files = list(audit_log.config.AUDIT_DIR.glob("*.jsonl"))
    path = files[0]
    lines = path.read_text().strip().splitlines()
    entry = json.loads(lines[0])
    entry["detail"]["a"] = 999
    path.write_text(json.dumps(entry) + "\n")
    ok, msg = verify.verify_audit_chain()
    assert not ok


def test_decision_chain_appends_and_verifies(isolated_data_dir):
    from grocery.audit import decisions as decision_log, verify
    decision_log.record(trace_id="t1", actor="price-scout", action="select_item", reasoning="cheapest")
    decision_log.record(trace_id="t1", actor="price-scout", action="select_item", reasoning="cheapest again")
    ok, msg = verify.verify_decision_chain()
    assert ok, msg


def test_trace_filters_by_trace_id(isolated_data_dir):
    from grocery.audit import decisions as decision_log
    decision_log.record(trace_id="order_A", actor="x", action="a", reasoning="r1")
    decision_log.record(trace_id="order_B", actor="x", action="a", reasoning="r2")
    decision_log.record(trace_id="order_A", actor="x", action="b", reasoning="r3")
    trace_a = decision_log.trace("order_A")
    assert len(trace_a) == 2
    assert all(s["trace_id"] == "order_A" for s in trace_a)
