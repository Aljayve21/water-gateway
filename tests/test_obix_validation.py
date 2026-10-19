"""Tests for gateway.obix_validation - validate oBIX values vs requirements."""

from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway.obix_validation import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    ObixRequirements,
    ObixValidator,
    load_requirements,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _rules(findings) -> set[str]:
    return {f.rule for f in findings}


def _row(**overrides) -> dict:
    row = {"meter_id": "m", "point_id": "p", "value": None, "text_value": None, "unit": ""}
    row.update(overrides)
    return row


# ── loader ───────────────────────────────────────────────────────────────
def test_missing_file_disables_checks():
    reqs = load_requirements(Path("definitely-not-here.yaml"))
    assert reqs.points == {}
    assert reqs.default_max_age_seconds is None


def test_load_requirements_parses_and_normalises(tmp_path):
    path = tmp_path / "req.yaml"
    path.write_text(
        textwrap.dedent(
            """
            defaults:
              max_age_seconds: 900
              severity:
                no_value: critical
            frozen:
              enabled: true
              buckets: 6
            unit_bounds:
              "obix:units/percent":
                min: 0
                max: 100
            points:
              "m/p":
                unit: "obix:units/celsius"
                min: 15
                max: 35
            """
        ),
        encoding="utf-8",
    )
    reqs = load_requirements(path)
    assert reqs.default_max_age_seconds == 900
    assert reqs.frozen_enabled is True
    assert reqs.frozen_buckets == 6
    # unit-bounds keys are normalised so `obix:units/percent` == `percent`
    assert reqs.unit_bounds.get("percent") == {"min": 0, "max": 100}
    assert reqs.points["m/p"]["unit"] == "obix:units/celsius"


def test_shipped_requirements_load():
    reqs = load_requirements()
    assert reqs.points, "config/obix_requirements.yaml should have point overrides"
    assert reqs.unit_bounds, "config/obix_requirements.yaml should have unit bounds"


# ── rules ───────────────────────────────────────────────────────────────
def test_no_value():
    validator = ObixValidator(ObixRequirements(default_severity={"no_value": SEVERITY_CRITICAL}))
    findings = validator.check(_row())
    assert "no_value" in _rules(findings)
    assert findings[0].severity == SEVERITY_CRITICAL


def test_no_value_not_raised_when_text_present():
    validator = ObixValidator(ObixRequirements())
    assert "no_value" not in _rules(validator.check(_row(text_value="open")))


def test_unit_mismatch():
    reqs = ObixRequirements(points={"m/p": {"unit": "obix:units/celsius"}})
    validator = ObixValidator(reqs)
    assert "unit_mismatch" in _rules(
        validator.check(_row(value=20, unit="obix:units/fahrenheit"))
    )
    assert "unit_mismatch" not in _rules(
        validator.check(_row(value=20, unit="obix:units/celsius"))
    )


def test_out_of_range_via_unit_bounds():
    reqs = ObixRequirements(unit_bounds={"percent": {"min": 0, "max": 100}})
    validator = ObixValidator(reqs)
    assert "out_of_range" in _rules(
        validator.check(_row(value=150, unit="obix:units/percent"))
    )
    assert "out_of_range" not in _rules(
        validator.check(_row(value=50, unit="obix:units/percent"))
    )


def test_point_override_tightens_unit_bounds():
    reqs = ObixRequirements(
        unit_bounds={"celsius": {"min": -20, "max": 80}},
        points={"m/p": {"min": 15, "max": 35}},
    )
    validator = ObixValidator(reqs)
    assert "out_of_range" in _rules(
        validator.check(_row(value=40, unit="obix:units/celsius"))
    )


def test_enum_invalid():
    reqs = ObixRequirements(points={"m/p": {"allowed": ["open", "closed"]}})
    validator = ObixValidator(reqs)
    assert "enum_invalid" in _rules(validator.check(_row(value=0, text_value="jammed")))
    assert "enum_invalid" not in _rules(validator.check(_row(value=0, text_value="OPEN")))


def test_stale():
    reqs = ObixRequirements(default_max_age_seconds=900)
    validator = ObixValidator(reqs)
    fresh = _row(value=1, captured_utc=(NOW - timedelta(minutes=5)).isoformat())
    old = _row(value=1, captured_utc=(NOW - timedelta(hours=2)).isoformat())
    assert "stale" not in _rules(validator.check(fresh, now=NOW))
    assert "stale" in _rules(validator.check(old, now=NOW))


def test_frozen_is_opt_in():
    buckets = [
        {"min_value": 0.0, "max_value": 0.0, "last_value": 0.0} for _ in range(3)
    ]
    row = _row(value=0.0)
    hist = lambda m, p: buckets  # noqa: E731

    on = ObixValidator(ObixRequirements(frozen_enabled=True, frozen_buckets=3))
    off = ObixValidator(ObixRequirements(frozen_enabled=False, frozen_buckets=3))
    assert "frozen" in _rules(on.check(row, history_for=hist))
    assert "frozen" not in _rules(off.check(row, history_for=hist))

    moving = buckets[:-1] + [{"min_value": 0.0, "max_value": 5.0, "last_value": 5.0}]
    assert "frozen" not in _rules(on.check(row, history_for=lambda m, p: moving))


def test_scan_walks_every_row():
    reqs = ObixRequirements(unit_bounds={"percent": {"min": 0, "max": 100}})
    validator = ObixValidator(reqs)
    rows = [
        _row(meter_id="m1", point_id="a", value=150, unit="obix:units/percent"),
        _row(meter_id="m1", point_id="b", value=50, unit="obix:units/percent"),
    ]
    findings = validator.scan(rows)
    assert [f.point_id for f in findings] == ["a"]


def test_finding_severity_defaults_to_warning():
    reqs = ObixRequirements(unit_bounds={"percent": {"min": 0, "max": 100}})
    validator = ObixValidator(reqs)
    findings = validator.check(_row(value=150, unit="obix:units/percent"))
    assert findings[0].severity == "warning"


def test_severity_override_from_point_spec():
    reqs = ObixRequirements(
        default_severity={"out_of_range": SEVERITY_INFO},
        unit_bounds={"percent": {"min": 0, "max": 100}},
        points={"m/p": {"severity": {"out_of_range": SEVERITY_CRITICAL}}},
    )
    validator = ObixValidator(reqs)
    findings = validator.check(_row(value=150, unit="obix:units/percent"))
    assert findings[0].severity == SEVERITY_CRITICAL
