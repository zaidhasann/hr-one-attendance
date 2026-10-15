from datetime import datetime, timezone

import pytest

from app.main import (
    build_department_summary_pipeline,
    build_employee_monthly_pipeline,
    build_late_leaderboard_pipeline,
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



def test_monthly_pipeline_contains_all_production_metrics():
    pipeline = build_employee_monthly_pipeline(
        "EMP001",
        parse_iso_date("2026-10-01"),
        parse_iso_date("2026-10-31"),
    )
    group = next(stage["$group"] for stage in pipeline if "$group" in stage)
    assert {"present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes"} <= set(group)


def test_department_summary_pipeline_keeps_zero_log_employees_and_computes_average():
    pipeline = build_department_summary_pipeline("2026-10-01", "2026-10-31", "Engineering")
    assert any("$lookup" in stage for stage in pipeline)
    assert any("$unwind" in stage and stage["$unwind"].get("preserveNullAndEmptyArrays") for stage in pipeline)
    project = next(stage["$project"] for stage in pipeline if "$project" in stage)
    assert "avg_work_hours" in project


def test_late_leaderboard_pipeline_includes_tie_ranking_and_department_filter():
    pipeline = build_late_leaderboard_pipeline("2026-10-01", "2026-10-31", 10, "Engineering")
    assert any("$setWindowFields" in stage for stage in pipeline)
    assert any("$rank" in str(stage) for stage in pipeline)
    assert any(stage.get("$match", {}).get("emp.department") == "Engineering" for stage in pipeline)
