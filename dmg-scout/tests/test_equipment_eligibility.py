"""Block 4B-prep Item 3: equipment-class eligibility
(app.pipeline.equipment_eligibility)."""
from app.models import Ab802Building, ProductLine, RetrofitBuilding
from app.pipeline.equipment_eligibility import (
    EquipmentClass, eligible_lines_for_equipment_class, equipment_class_coverage_report,
    equipment_class_from_retrofit_type,
)


class TestEquipmentClassFromRetrofitType:
    def test_maps_every_known_retrofit_equipment_type(self):
        cases = [
            ("packaged_rooftop", EquipmentClass.rooftop_packaged),
            ("split_dx", EquipmentClass.split_dx),
            ("water_cooled_chiller", EquipmentClass.chiller),
            ("air_cooled_chiller", EquipmentClass.chiller),
            ("boiler", EquipmentClass.boiler),
            ("cooling_tower", EquipmentClass.cooling_tower),
            ("air_handling_unit", EquipmentClass.ahu),
        ]
        for equipment_type, expected in cases:
            assert equipment_class_from_retrofit_type(equipment_type) == expected

    def test_vav_terminal_has_no_home_and_abstains(self):
        """A VAV terminal is a distribution/mixing box, not any of the
        eight requested equipment classes -- never forced into `ahu` just
        because both touch air handling."""
        assert equipment_class_from_retrofit_type("vav_terminal") == EquipmentClass.unknown

    def test_null_equipment_type_abstains(self):
        assert equipment_class_from_retrofit_type(None) == EquipmentClass.unknown

    def test_unrecognized_string_abstains_not_guessed(self):
        assert equipment_class_from_retrofit_type("some_new_type_ladbs_never_saw_before") == EquipmentClass.unknown


class TestEligibleLinesForEquipmentClass:
    def test_unknown_never_returns_a_line(self, db_session):
        db_session.add(ProductLine(name="Test AHU", name_norm="test ahu", category="air_handling",
                                   building_role="air_handling"))
        db_session.commit()
        assert eligible_lines_for_equipment_class(db_session, EquipmentClass.unknown) == []

    def test_ahu_returns_an_air_handling_role_line(self, db_session):
        db_session.add(ProductLine(name="Test AHU", name_norm="test ahu", category="air_handling",
                                   building_role="air_handling"))
        db_session.add(ProductLine(name="Test Boiler", name_norm="test boiler", category="heaters",
                                   building_role="heating_specialty"))
        db_session.commit()
        lines = eligible_lines_for_equipment_class(db_session, EquipmentClass.ahu)
        assert [l.name for l in lines] == ["Test AHU"]

    def test_vrf_and_split_dx_and_chiller_all_share_cooling_generation(self, db_session):
        db_session.add(ProductLine(name="Test VRF Line", name_norm="test vrf line", category="vrf_split",
                                   building_role="cooling_generation"))
        db_session.commit()
        for equipment_class in (EquipmentClass.vrf, EquipmentClass.split_dx, EquipmentClass.chiller):
            lines = eligible_lines_for_equipment_class(db_session, equipment_class)
            assert [l.name for l in lines] == ["Test VRF Line"]

    def test_osp_required_excludes_a_line_with_no_current_osp(self, db_session):
        db_session.add(ProductLine(name="No OSP Chiller", name_norm="no osp chiller", category="chillers_cooling",
                                   building_role="cooling_generation", oshpd_osp=False))
        db_session.commit()
        assert eligible_lines_for_equipment_class(db_session, EquipmentClass.chiller, osp_required=True) == []

    def test_osp_required_includes_a_line_with_a_current_osp(self, db_session):
        db_session.add(ProductLine(name="OSP Chiller", name_norm="osp chiller", category="chillers_cooling",
                                   building_role="cooling_generation", oshpd_osp=True))
        db_session.commit()
        lines = eligible_lines_for_equipment_class(db_session, EquipmentClass.chiller, osp_required=True)
        assert [l.name for l in lines] == ["OSP Chiller"]


class TestEquipmentClassCoverageReport:
    def test_reports_zero_coverage_for_ab802_by_construction(self, db_session):
        db_session.add(Ab802Building(portfolio_manager_property_id="1", year_ending=2025,
                                     address_1="1 Test Way", city="LA", state_province="CA",
                                     source_url="https://x"))
        db_session.commit()
        report = equipment_class_coverage_report(db_session)
        assert report["ab802_buildings"]["total"] == 1
        assert report["ab802_buildings"]["coverage_pct"] == 0.0

    def test_replacement_candidate_population_always_has_zero_known_class(self, db_session):
        """By design: absence of a permit is the signal for this
        population, so equipment_type -- and therefore equipment class --
        is always null."""
        db_session.add(RetrofitBuilding(apn="1-1-1", population="replacement_candidate"))
        db_session.commit()
        report = equipment_class_coverage_report(db_session)
        assert report["retrofit_buildings"]["replacement_candidate"]["known_equipment_class"] == 0

    def test_recently_active_population_reports_real_coverage(self, db_session):
        db_session.add(RetrofitBuilding(apn="2-2-2", population="recently_active", equipment_type="boiler"))
        db_session.add(RetrofitBuilding(apn="3-3-3", population="recently_active", equipment_type=None))
        db_session.commit()
        report = equipment_class_coverage_report(db_session)
        stats = report["retrofit_buildings"]["recently_active"]
        assert stats["total"] == 2
        assert stats["known_equipment_class"] == 1
        assert stats["coverage_pct"] == 50.0
        assert stats["by_class"]["boiler"] == 1
        assert stats["by_class"]["unknown"] == 1
