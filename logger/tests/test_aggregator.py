"""MinuteAggregator: mean, None handling, circular (WDIR) averaging, reset."""
from datetime import datetime, timezone

from aqms_logger.aggregator import MinuteAggregator, circular_mean
from aqms_logger.models import Reading, STATUS_COMM_FAIL, STATUS_VALID

M = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)


def R(param, value, unit="ug/m3", status=STATUS_VALID, device="fidas200", station="TEST"):
    return Reading(datetime.now(timezone.utc), station, device, param, value, unit, status)


def test_mean_of_samples():
    agg = MinuteAggregator()
    for v in (10.0, 20.0, 30.0):
        agg.add([R("PM2_5", v)])
    out = {r.parameter: r for r in agg.build(M)}
    assert out["PM2_5"].value == 20.0
    assert out["PM2_5"].status == STATUS_VALID
    assert out["PM2_5"].time == M and out["PM2_5"].unit == "ug/m3"


def test_none_samples_ignored():
    agg = MinuteAggregator()
    agg.add([R("PM10", 10.0)])
    agg.add([R("PM10", None, status=STATUS_COMM_FAIL)])
    agg.add([R("PM10", 20.0)])
    out = {r.parameter: r for r in agg.build(M)}
    assert out["PM10"].value == 15.0            # mean of 10 & 20, None skipped


def test_all_none_is_comm_fail():
    agg = MinuteAggregator()
    agg.add([R("PM1", None, status=STATUS_COMM_FAIL)])
    agg.add([R("PM1", None, status=STATUS_COMM_FAIL)])
    out = {r.parameter: r for r in agg.build(M)}
    assert out["PM1"].value is None and out["PM1"].status == STATUS_COMM_FAIL


def test_circular_mean_for_wdir():
    agg = MinuteAggregator(circular_params={"WDIR"})
    agg.add([R("WDIR", 350.0, unit="deg")])
    agg.add([R("WDIR", 10.0, unit="deg")])
    v = {r.parameter: r for r in agg.build(M)}["WDIR"].value
    assert min(v, 360.0 - v) < 1e-6             # ≈0°, not the linear-mean 180°


def test_last_mode_reports_current_state():
    # Discrete state codes (e.g. MODUS) use the most recent sample, not a mean.
    agg = MinuteAggregator(last_params={"MODUS"})
    for code in (1.0, 1.0, 2.0):          # auto, auto, manual
        agg.add([R("MODUS", code, unit="code")])
    out = {r.parameter: r for r in agg.build(M)}
    assert out["MODUS"].value == 2.0      # last state, not mean 1.33
    assert out["MODUS"].status == STATUS_VALID


def test_last_mode_skips_none_samples():
    # A comm-fail sample must not become the reported "current state".
    agg = MinuteAggregator(last_params={"MODUS"})
    agg.add([R("MODUS", 2.0, unit="code")])
    agg.add([R("MODUS", None, unit="code", status=STATUS_COMM_FAIL)])
    out = {r.parameter: r for r in agg.build(M)}
    assert out["MODUS"].value == 2.0      # last *valid* sample


def test_max_mode_latches_flag():
    # A brief coincidence error anywhere in the minute must survive (max, not mean).
    agg = MinuteAggregator(max_params={"COINCIDENCE"})
    for code in (0.0, 1.0, 0.0):
        agg.add([R("COINCIDENCE", code, unit="code")])
    out = {r.parameter: r for r in agg.build(M)}
    assert out["COINCIDENCE"].value == 1.0


def test_build_resets():
    agg = MinuteAggregator()
    agg.add([R("PM2_5", 5.0)])
    agg.build(M)
    assert agg.build(M) == []                   # accumulator cleared


def test_circular_mean_helper():
    assert circular_mean([]) is None
    assert abs(circular_mean([90.0, 90.0, 90.0]) - 90.0) < 1e-6
