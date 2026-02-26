from probe import RollingMetrics, classify_reason


def _metrics(*, target_lat=None, public_lat=None, public_ok=None):
    m = RollingMetrics()
    if target_lat:
        for v in target_lat:
            m.target_latency_window.append(float(v))
    if public_lat:
        for v in public_lat:
            m.public_latency_window.append(float(v))
    if public_ok:
        for v in public_ok:
            m.public_ok_window.append(bool(v))
    return m


def test_dsl_sync_drop_has_priority():
    reason, details = classify_reason(
        in_outage=True,
        dsl_sync_up=False,
        fritz_ping_ok=True,
        public_ping_ok=False,
        dns_ok=False,
        metrics=_metrics(target_lat=[10] * 20, public_lat=[10] * 20, public_ok=[True] * 20),
    )
    assert reason == "DSL_SYNC_DROP"
    assert details["dsl_sync_up"] is False


def test_wan_reachability_loss_with_sync_ok():
    reason, _ = classify_reason(
        in_outage=True,
        dsl_sync_up=True,
        fritz_ping_ok=True,
        public_ping_ok=False,
        dns_ok=True,
        metrics=_metrics(target_lat=[10] * 20, public_lat=[10] * 20, public_ok=[False] * 20),
    )
    assert reason == "WAN_REACHABILITY_LOSS_WITH_SYNC_OK"


def test_high_latency_under_upload_not_outage():
    # p95 well above threshold
    lats = [20] * 5 + [250] * 20
    reason, details = classify_reason(
        in_outage=False,
        dsl_sync_up=None,
        fritz_ping_ok=True,
        public_ping_ok=True,
        dns_ok=True,
        metrics=_metrics(target_lat=lats, public_lat=lats, public_ok=[True] * len(lats)),
    )
    assert reason == "HIGH_LATENCY_UNDER_UPLOAD"

    latency = details.get("latency")
    assert isinstance(latency, dict)
    assert latency.get("target_p95_ms") is not None

