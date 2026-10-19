"""Parsing tests for the oBIX client.

Covers the two point shapes seen on real servers: a value element inside a
point object (mock/legacy) and the value as the document root (Niagara), plus
the Niagara href listing where refs carry only a ``display`` string.
"""

from __future__ import annotations

import pytest

from gateway.obix import ObixError, parse_point, parse_refs

NIAGARA_POINT = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<real val="1139.3740234375" '
    'href="https://192.168.0.120/obix/config/Drivers/BacnetNetwork/'
    'ENYECONTROLS/points/GF_Water_M$20Reading_AI19/" '
    'is="/obix/def/control:NumericPoint obix:Point" '
    'unit="obix:units/cubic meter"/>'
)

WRAPPED_POINT = (
    '<obj name="totalizer">'
    '<real name="totalizer" val="42.5" is="obix:Real" unit="obix:units/cubic meter"/>'
    '</obj>'
)

NIAGARA_REFS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<obj href="https://192.168.0.120/obix/config/Drivers/BacnetNetwork/'
    'ENYECONTROLS/points/">'
    '<ref name="GF_Water_M$20Reading_AI19" '
    'href="GF_Water_M$20Reading_AI19/" '
    'is="/obix/def/control:NumericPoint obix:Point" display="1139.37 m&#xb3; {ok}"/>'
    '<ref name="SomeFolder" href="SomeFolder/" is="/obix/def/baja:Folder" display="Folder"/>'
    '</obj>'
)

ERR_DOC = (
    '<err href="https://x/obix/nope/" is="obix:BadUriErr" '
    'display="Cannot find lobby agent for nope"/>'
)


def test_value_as_root_is_parsed() -> None:
    point = parse_point(NIAGARA_POINT)
    assert point.value == "1139.3740234375"
    assert point.as_float == pytest.approx(1139.3740234375)
    assert point.unit == "obix:units/cubic meter"
    assert point.href and point.href.endswith("GF_Water_M$20Reading_AI19/")


def test_value_wrapped_in_object_is_parsed() -> None:
    point = parse_point(WRAPPED_POINT)
    assert point.value == "42.5"
    assert point.as_float == 42.5


def test_error_document_raises() -> None:
    with pytest.raises(ObixError, match="BadUriErr|Cannot find"):
        parse_point(ERR_DOC)


def test_refs_keep_name_href_and_display() -> None:
    refs = parse_refs(NIAGARA_REFS)
    assert len(refs) == 2
    first = refs[0]
    assert first.name == "GF_Water_M$20Reading_AI19"
    assert first.href == "GF_Water_M$20Reading_AI19/"
    assert first.value == "1139.37 m\u00b3 {ok}"
    assert first.is_type.endswith("NumericPoint obix:Point")
