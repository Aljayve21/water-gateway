"""Tests for the equipment <-> point naming map and the read-API metadata.

The map is optional by design, so most of what is asserted here is the
*fallback* behaviour: an unmapped device or point must still resolve to a
stable, human-readable address rather than an error.
"""

from __future__ import annotations

import pytest

from gateway.equipment import EquipmentMap


class TestEquipmentMapLoading:
    def test_missing_file_is_an_empty_but_valid_map(self, tmp_path):
        emap = EquipmentMap.load(tmp_path / "nope.yaml")
        assert emap.devices == {}
        assert emap.points == {}

    def test_load_reads_devices_and_points(self, tmp_path):
        path = tmp_path / "equipment_map.yaml"
        path.write_text(
            "devices:\n"
            "  bms_enyecontrols: ENYECONTROLS\n"
            "points:\n"
            "  'bms_enyecontrols/temp': SpaceTemp\n",
            encoding="utf-8",
        )
        emap = EquipmentMap.load(path)
        assert emap.devices == {"bms_enyecontrols": "ENYECONTROLS"}
        assert emap.points[("bms_enyecontrols", "temp")] == "SpaceTemp"

    def test_empty_values_are_skipped(self, tmp_path):
        path = tmp_path / "equipment_map.yaml"
        path.write_text(
            "devices:\n  bms_enyecontrols: ''\npoints:\n  'bms_enyecontrols/temp': \n",
            encoding="utf-8",
        )
        emap = EquipmentMap.load(path)
        assert emap.devices == {}
        assert emap.points == {}

    def test_non_mapping_file_raises(self, tmp_path):
        path = tmp_path / "equipment_map.yaml"
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError):
            EquipmentMap.load(path)

    def test_shipped_config_is_valid(self):
        # The repo's own config/equipment_map.yaml must always load.
        emap = EquipmentMap.load()
        assert "bms_enyecontrols" in emap.devices


class TestForwardResolution:
    def test_mapped_device_uses_the_mapped_name(self):
        emap = EquipmentMap.from_dict({"devices": {"bms_x": "AHU-01"}})
        assert emap.equipment_id("bms_x") == "AHU-01"

    def test_unmapped_device_falls_back_to_station_name(self):
        emap = EquipmentMap()
        emap.with_station_names(devices=[{"meter_id": "bms_x", "name": "ENYECONTROLS"}])
        assert emap.equipment_id("bms_x") == "ENYECONTROLS"

    def test_unmapped_device_with_no_station_name_falls_back_to_id(self):
        emap = EquipmentMap()
        assert emap.equipment_id("bms_x") == "bms_x"

    def test_mapped_point_wins_over_station_name(self):
        emap = EquipmentMap.from_dict(
            {"points": {"bms_enyecontrols/temp": "SpaceTemp"}}
        )
        emap.with_station_names(
            points=[{"meter_id": "bms_enyecontrols", "point_id": "temp", "name": "raw"}]
        )
        assert emap.equipment_point("bms_enyecontrols", "temp") == "SpaceTemp"

    def test_point_lookup_is_case_insensitive(self):
        emap = EquipmentMap.from_dict({"points": {"BMS_X/Temp": "SpaceTemp"}})
        assert emap.equipment_point("bms_x", "temp") == "SpaceTemp"

    def test_resolve_returns_both_names(self):
        emap = EquipmentMap.from_dict({"devices": {"bms_x": "AHU-01"}})
        assert emap.resolve("bms_x", "t1") == {
            "equipment_id": "AHU-01",
            "equipment_point": "t1",
        }

    def test_is_mapped_only_for_explicit_entries(self):
        emap = EquipmentMap.from_dict(
            {"devices": {"bms_x": "AHU-01"}, "points": {"bms_x/t1": "SpaceTemp"}}
        )
        assert emap.is_mapped("bms_x", "t1")
        assert emap.is_mapped("bms_x", "other")  # device-level mapping
        assert not emap.is_mapped("bms_y", "t1")


class TestReverseLookup:
    def test_mapped_pair_resolves_back_to_station_ids(self):
        emap = EquipmentMap.from_dict(
            {
                "devices": {"bms_enyecontrols": "ENYECONTROLS"},
                "points": {"bms_enyecontrols/temp": "SpaceTemp"},
            }
        )
        assert emap.to_station("ENYECONTROLS", "SpaceTemp") == (
            "bms_enyecontrols",
            "temp",
        )

    def test_station_names_resolve_when_unmapped(self):
        emap = EquipmentMap()
        emap.with_station_names(
            devices=[{"meter_id": "bms_x", "name": "ENYECONTROLS"}],
            points=[{"meter_id": "bms_x", "point_id": "temp", "name": "Temp"}],
        )
        assert emap.to_station("ENYECONTROLS", "Temp") == ("bms_x", "temp")

    def test_unknown_address_returns_none(self):
        emap = EquipmentMap.from_dict({"devices": {"bms_x": "AHU-01"}})
        assert emap.to_station("AHU-99", "Nope") is None

    def test_lookup_is_case_insensitive(self):
        emap = EquipmentMap.from_dict(
            {
                "devices": {"bms_x": "AHU-01"},
                "points": {"bms_x/temp": "SpaceTemp"},
            }
        )
        assert emap.to_station("ahu-01", "spacetemp") == ("bms_x", "temp")

    def test_bare_meter_id_also_works(self):
        # A caller may pass the raw meter_id in place of the equipment id.
        emap = EquipmentMap.from_dict(
            {
                "devices": {"bms_x": "AHU-01"},
                "points": {"bms_x/temp": "SpaceTemp"},
            }
        )
        assert emap.to_station("bms_x", "SpaceTemp") == ("bms_x", "temp")

    def test_every_forward_name_round_trips(self):
        # What resolve() hands out must always resolve back, for mapped and
        # unmapped points alike. This is the property the API depends on.
        emap = EquipmentMap.from_dict({"devices": {"bms_x": "AHU-01"}})
        emap.with_station_names(
            devices=[{"meter_id": "bms_x", "name": "AHU-01"}],
            points=[
                {"meter_id": "bms_x", "point_id": "temp", "name": "SpaceTemp"},
                # A station whose "name" is just the point id - the live case.
                {"meter_id": "bms_x", "point_id": "flow", "name": "flow"},
            ],
        )
        for point_id in ("temp", "flow"):
            names = emap.resolve("bms_x", point_id)
            assert emap.to_station(
                names["equipment_id"], names["equipment_point"]
            ) == ("bms_x", point_id)

    def test_add_station_point_round_trips_without_full_catalog(self):
        emap = EquipmentMap.from_dict({"devices": {"bms_x": "AHU-01"}})
        emap.add_station_point("bms_x", "flow", "flow")
        names = emap.resolve("bms_x", "flow")
        assert names == {"equipment_id": "AHU-01", "equipment_point": "flow"}
        assert emap.to_station("AHU-01", "flow") == ("bms_x", "flow")


class TestSummary:
    def test_summary_counts_both_layers(self):
        emap = EquipmentMap.from_dict(
            {
                "devices": {"bms_x": "AHU-01"},
                "points": {"bms_x/temp": "SpaceTemp"},
            }
        )
        emap.with_station_names(
            devices=[{"meter_id": "bms_y", "name": "METER"}],
            points=[{"meter_id": "bms_y", "point_id": "p", "name": "P"}],
        )
        summary = emap.summary()
        assert summary["devices_mapped"] == 1
        assert summary["points_mapped"] == 1
        assert summary["station_devices"] == 1
        assert summary["station_points"] == 1
