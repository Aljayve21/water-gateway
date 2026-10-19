"""Tests for the Neoview facet probe (gateway.obix / obix_facets / obix_validation)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gateway.obix import Facets, ObixClient, ObixError, parse_facets
from gateway.obix_facets import (
    iter_read_points,
    load_facets_cache,
    probe_facets,
    save_facets_cache,
)
from gateway.obix_validation import ObixRequirements, ObixValidator, load_requirements

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _row(**overrides) -> dict:
    row = {
        "meter_id": "m",
        "point_id": "p",
        "value": None,
        "text_value": None,
        "unit": "",
        "captured_utc": NOW.isoformat(),
    }
    row.update(overrides)
    return row


def _rules(findings) -> set[str]:
    return {f.rule for f in findings}


# ── parse_facets ─────────────────────────────────────────────────────────
def test_parse_facets_niagara_real():
    facets = parse_facets(
        '<real val="23.5" unit="obix:units/celsius" min="0" max="100" '
        'precision="0.1" is="obix:Point"/>'
    )
    assert facets.unit == "obix:units/celsius"
    assert facets.min_value == 0.0
    assert facets.max_value == 100.0
    assert facets.precision == 0.1
    assert facets.has_bounds
    assert not facets.is_empty


def test_parse_facets_wrapped_object():
    facets = parse_facets(
        '<obj name="Ai/Site1/T">'
        '<real val="5" min="1" max="9" unit="obix:units/volt"/>'
        "</obj>"
    )
    assert facets.min_value == 1.0
    assert facets.max_value == 9.0
    assert facets.unit == "obix:units/volt"


def test_parse_facets_without_facets_is_empty():
    facets = parse_facets('<real val="5"/>')
    assert facets.is_empty
    assert facets.as_dict() == {}


def test_parse_facets_ignores_unparseable_numbers():
    facets = parse_facets('<real val="5" min="abc" max="10"/>')
    assert facets.min_value is None
    assert facets.max_value == 10.0


def test_parse_facets_error_element_raises():
    with pytest.raises(ObixError):
        parse_facets('<err display="no such point"/>')


def test_facets_as_dict_keeps_only_present():
    facets = Facets(unit="obix:units/percent", min_value=0.0, max_value=100.0)
    assert facets.as_dict() == {
        "unit": "obix:units/percent",
        "min": 0.0,
        "max": 100.0,
    }


# ── ObixClient.get_facets ────────────────────────────────────────────────
def _client_returning(responses: dict):
    client = ObixClient("https://192.168.0.120/obix", username="Obixuser", password="x")

    def fake_request(path, method="GET", body=None):
        if path in responses:
            return responses[path]
        raise ObixError(f"404 {path}")

    client._request = fake_request  # type: ignore[assignment]
    return client


def test_get_facets_reads_point_object():
    client = _client_returning(
        {"/p": '<real val="1" min="0" max="10" unit="obix:units/volt"/>'}
    )
    facets = client.get_facets("/p")
    assert facets.max_value == 10.0
    assert facets.unit == "obix:units/volt"


def test_get_facets_falls_back_to_value_child():
    client = _client_returning(
        {"/p": '<real val="1"/>', "/p/value": '<real val="1" min="2" max="3"/>'}
    )
    facets = client.get_facets("/p")
    assert facets.min_value == 2.0
    assert facets.max_value == 3.0


def test_get_facets_raises_when_both_candidates_fail():
    client = _client_returning({})
    with pytest.raises(ObixError):
        client.get_facets("/p")


def test_get_facets_returns_empty_for_a_point_without_facets():
    # A bool/occupancy point: the object reads fine but has no facets, and the
    # /value fallback 404s. That is "no facets", not a failure.
    client = _client_returning({"/p": '<bool val="true"/>'})
    facets = client.get_facets("/p")
    assert facets.is_empty


# ── iter_read_points ─────────────────────────────────────────────────────
def test_iter_read_points_filters_meter():
    point_map = {
        "read": {
            "m1": {"a": "/a", "b": "/b"},
            "m2": {"c": "/c"},
        }
    }
    assert list(iter_read_points(point_map)) == [
        ("m1", "a", "/a"),
        ("m1", "b", "/b"),
        ("m2", "c", "/c"),
    ]
    assert list(iter_read_points(point_map, meter_id="m2")) == [("m2", "c", "/c")]


def test_iter_read_points_skips_empty_href():
    point_map = {"read": {"m1": {"a": "/a", "b": ""}}}
    assert list(iter_read_points(point_map)) == [("m1", "a", "/a")]


# ── probe_facets ─────────────────────────────────────────────────────────
class _FakeClient:
    """Maps an href to a Facets, ``None`` (no facets) or ``"error"``."""

    def __init__(self, behaviour: dict):
        self._behaviour = behaviour

    def get_facets(self, path, *, raw=True):
        result = self._behaviour.get(path)
        if result == "error":
            raise ObixError("cannot reach oBIX station")
        if result is None:
            return Facets()
        return result


POINT_MAP = {
    "read": {
        "m1": {"a": "/a", "b": "/b"},
        "m2": {"c": "/c"},
    }
}


def test_probe_collects_facets_and_errors():
    client = _FakeClient(
        {
            "/a": Facets(unit="obix:units/volt", min_value=0.0, max_value=10.0),
            "/b": Facets(),
            "/c": "error",
        }
    )
    report = probe_facets(client, POINT_MAP)
    assert report.probed == 3
    assert report.with_facets == 1
    assert report.without_facets == 1
    assert len(report.errors) == 1
    assert not report.aborted
    assert report.points == {
        "m1/a": {"unit": "obix:units/volt", "min": 0.0, "max": 10.0}
    }


def test_probe_aborts_after_consecutive_transport_errors():
    client = _FakeClient({"/a": "error", "/b": "error", "/c": "error"})
    report = probe_facets(client, POINT_MAP, abort_after_errors=2)
    assert report.aborted
    assert report.probed == 2
    assert len(report.errors) == 2
    assert report.points == {}


def test_probe_continues_after_a_recovered_error():
    client = _FakeClient(
        {
            "/a": "error",
            "/b": Facets(min_value=1.0, max_value=2.0),
            "/c": Facets(min_value=3.0, max_value=4.0),
        }
    )
    report = probe_facets(client, POINT_MAP, abort_after_errors=2)
    assert not report.aborted
    assert report.probed == 3
    assert report.with_facets == 2
    assert set(report.points) == {"m1/b", "m2/c"}


def test_probe_does_not_abort_after_a_success():
    # A reachable station with a few bad points must keep going, not abort.
    point_map = {"read": {"m1": {p: f"/{p}" for p in "abcde"}}}
    client = _FakeClient(
        {
            "/a": Facets(min_value=1.0, max_value=2.0),
            "/b": "error",
            "/c": "error",
            "/d": "error",
        }
    )
    report = probe_facets(client, point_map, abort_after_errors=2)
    assert not report.aborted
    assert report.probed == 5
    assert report.with_facets == 1
    assert len(report.errors) == 3
    assert report.points == {"m1/a": {"min": 1.0, "max": 2.0}}


# ── cache ────────────────────────────────────────────────────────────────
def test_facet_cache_round_trip(tmp_path):
    target = tmp_path / "facets.json"
    points = {"m1/a": {"min": 0.0, "max": 10.0, "unit": "obix:units/volt"}}
    save_facets_cache(points, str(target), source="https://station/obix")
    assert load_facets_cache(str(target)) == points


def test_load_facets_cache_missing_returns_empty(tmp_path):
    assert load_facets_cache(str(tmp_path / "nope.json")) == {}


def test_load_facets_cache_tolerates_garbage(tmp_path):
    target = tmp_path / "facets.json"
    target.write_text("not json", encoding="utf-8")
    assert load_facets_cache(str(target)) == {}


# ── validation: facet layer ──────────────────────────────────────────────
def test_facet_bounds_override_unit_bounds():
    reqs = ObixRequirements(
        unit_bounds={"celsius": {"min": -50, "max": 200}},
        facets={"m/p": {"min": 15.0, "max": 35.0}},
    )
    findings = ObixValidator(reqs).check(_row(value=40.0, unit="obix:units/celsius"))
    assert "out_of_range" in _rules(findings)
    hit = next(f for f in findings if f.rule == "out_of_range")
    assert hit.detail["min"] == 15.0
    assert hit.detail["max"] == 35.0


def test_degenerate_facet_bounds_are_ignored():
    # Neoview reports min == max (often 0/0) when a point's engineering range
    # was never set; that must not turn every reading into an out_of_range.
    reqs = ObixRequirements(facets={"m/p": {"min": 0.0, "max": 0.0}})
    assert "out_of_range" not in _rules(ObixValidator(reqs).check(_row(value=5.0)))


def test_explicit_point_entry_overrides_facets():
    reqs = ObixRequirements(
        facets={"m/p": {"min": 0.0, "max": 100.0}},
        points={"m/p": {"min": 15, "max": 35}},
    )
    findings = ObixValidator(reqs).check(_row(value=40.0, unit="obix:units/celsius"))
    hit = next(f for f in findings if f.rule == "out_of_range")
    assert hit.detail["min"] == 15.0
    assert hit.detail["max"] == 35.0


def test_facet_precision_becomes_tolerance():
    reqs = ObixRequirements(
        facets={"m/p": {"min": 0.0, "max": 100.0, "precision": 1.0}}
    )
    validator = ObixValidator(reqs)
    assert "out_of_range" not in _rules(validator.check(_row(value=100.5)))
    assert "out_of_range" in _rules(validator.check(_row(value=101.5)))


def test_facet_precision_ignored_when_disabled():
    reqs = ObixRequirements(
        facets={"m/p": {"min": 0.0, "max": 100.0, "precision": 1.0}},
        facets_use_precision=False,
    )
    assert "out_of_range" in _rules(ObixValidator(reqs).check(_row(value=100.5)))


def test_facet_unit_sets_required_unit():
    reqs = ObixRequirements(facets={"m/p": {"unit": "obix:units/celsius"}})
    findings = ObixValidator(reqs).check(
        _row(value=20.0, unit="obix:units/fahrenheit")
    )
    assert "unit_mismatch" in _rules(findings)


def test_facet_unit_not_trusted_when_disabled():
    reqs = ObixRequirements(
        facets={"m/p": {"unit": "obix:units/celsius"}}, facets_trust_unit=False
    )
    findings = ObixValidator(reqs).check(
        _row(value=20.0, unit="obix:units/fahrenheit")
    )
    assert "unit_mismatch" not in _rules(findings)


# ── load_requirements: facet wiring ─────────────────────────────────────
def _write_yaml(tmp_path, body: str):
    path = tmp_path / "req.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_requirements_uses_explicit_facets(tmp_path):
    path = _write_yaml(
        tmp_path,
        "defaults: {}\nunit_bounds: {}\npoints: {}\nfacets:\n  enabled: false\n",
    )
    reqs = load_requirements(path, facets={"m/p": {"min": 1.0, "max": 2.0}})
    assert reqs.facets == {"m/p": {"min": 1.0, "max": 2.0}}


def test_load_requirements_auto_loads_cache(tmp_path):
    cache = tmp_path / "facets.json"
    save_facets_cache({"m/p": {"min": 1.0, "max": 2.0}}, str(cache))
    path = _write_yaml(
        tmp_path,
        f"defaults: {{}}\nfacets:\n  enabled: true\n  cache: \"{cache.as_posix()}\"\n",
    )
    reqs = load_requirements(path)
    assert reqs.facets == {"m/p": {"min": 1.0, "max": 2.0}}


def test_load_requirements_ignores_cache_when_disabled(tmp_path):
    cache = tmp_path / "facets.json"
    save_facets_cache({"m/p": {"min": 1.0, "max": 2.0}}, str(cache))
    path = _write_yaml(
        tmp_path,
        f"defaults: {{}}\nfacets:\n  enabled: false\n  cache: \"{cache.as_posix()}\"\n",
    )
    assert load_requirements(path).facets == {}


def test_shipped_requirements_load_with_facet_config():
    reqs = load_requirements()
    assert reqs.points, "config/obix_requirements.yaml should have point overrides"
    assert isinstance(reqs.facets, dict)
    assert reqs.facets_use_precision is True
    assert reqs.facets_trust_unit is True
