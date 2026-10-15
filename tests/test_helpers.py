from datetime import datetime, timezone

import pytest

from app.main import (
    build_department_trend_pipeline,
    get_attendance_date_for_punch,
    parse_iso_date,
    round_half_up,
    validate_epoch_ms,
)


def test_parse_iso_date_accepts_calendar_date():
    assert parse_iso_date("2026-10-15").isoformat() == "2026-10-15"


@pytest.mark.parametrize("value", [
    "2026-1-15",
    "2026-10-5",
    "2026-10-15T00:00:00",
    "15-10-2026",
    "",
    None,
])
def test_parse_iso_date_rejects_noncanonical_values(value):
    with pytest.raises(ValueError):
        parse_iso_date(value)


def test_round_half_up_avoids_bankers_rounding():
    assert round_half_up(2.675, 2) == 2.68
    assert round_half_up(4.495, 2) == 4.5


def test_epoch_ms_rejects_seconds_and_booleans():
    with pytest.raises(ValueError):
        validate_epoch_ms(1_760_000_000)
    with pytest.raises(ValueError):
        validate_epoch_ms(True)


def test_overnight_shift_assigns_early_morning_punch_to_previous_date():
    # 00:15 UTC is 05:45 IST, before the 06:00 IST shift end.
    punch = datetime(2026, 10, 15, 0, 15, tzinfo=timezone.utc)
    assert get_attendance_date_for_punch(punch, "22:00", "06:00") == "2026-10-14"


def test_department_trend_pipeline_pushes_date_filter_into_lookup_and_computes_window():
    pipeline = build_department_trend_pipeline("Engineering", "2026-10-01", "2026-10-07", 7)
    lookup = next(stage["$lookup"] for stage in pipeline if "$lookup" in stage)
    assert lookup["from"] == "attendance_logs"
    assert any("$gte" in str(stage) and "2026-10-01" in str(stage) for stage in lookup["pipeline"])
    assert any("$lte" in str(stage) and "2026-10-07" in str(stage) for stage in lookup["pipeline"])
    assert any("$setWindowFields" in stage for stage in pipeline)
