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


def _line(name, category, role, osp=None):
    return ProductLine(name=name, name_norm=name.lower(), category=category, building_role=role, oshpd_osp=osp)


class TestEligibleLinesForEquipmentClass:
    def test_unknown_never_returns_a_line(self, db_session):
        db_session.add(_line("AAON", "rooftop_units", "air_handling"))
        db_session.commit()
        assert eligible_lines_for_equipment_class(db_session, EquipmentClass.unknown) == []

    def test_boiler_never_returns_a_line_yet(self, db_session):
        """ABSTAIN until a boiler line is confirmed -- never falls back to
        a heating_specialty-role line like Cambridge/Suburban/Markel/IEC
        just because it shares a role tag with real boiler equipment."""
        db_session.add(_line("Cambridge", "heaters", "heating_specialty"))
        db_session.commit()
        assert eligible_lines_for_equipment_class(db_session, EquipmentClass.boiler) == []

    def test_ahu_returns_exactly_energy_labs_aaon_climatecraft(self, db_session):
        db_session.add(_line("Energy Labs", "air_handling", "air_handling", osp=True))
        db_session.add(_line("AAON", "rooftop_units", "air_handling", osp=True))
        db_session.add(_line("ClimateCraft", "air_handling", "air_handling", osp=True))
        db_session.add(_line("BASX", "air_handling", "air_handling"))  # a real air_handling line NOT on this list
        db_session.commit()
        names = {l.name for l in eligible_lines_for_equipment_class(db_session, EquipmentClass.ahu)}
        assert names == {"Energy Labs", "AAON", "ClimateCraft"}

    def test_rooftop_packaged_returns_exactly_aaon_lg(self, db_session):
        db_session.add(_line("AAON", "rooftop_units", "air_handling", osp=True))
        db_session.add(_line("LG", "vrf_split", "cooling_generation"))
        db_session.commit()
        names = {l.name for l in eligible_lines_for_equipment_class(db_session, EquipmentClass.rooftop_packaged)}
        assert names == {"AAON", "LG"}

    def test_split_dx_returns_lg_and_the_other_vrf_split_lines_not_climacool(self, db_session):
        """The bug this replaces: split_dx and chiller both shared
        "cooling_generation" under the old role-based lookup, so
        ClimaCool (a chiller line) was wrongly offered for a split-DX
        replacement -- confirmed on 6 of Block 4B-prep-2's ten real
        promotions."""
        db_session.add(_line("LG", "vrf_split", "cooling_generation"))
        db_session.add(_line("ClimateMaster", "vrf_split", "cooling_generation"))
        db_session.add(_line("Islandaire", "vrf_split", "cooling_generation"))
        db_session.add(_line("Hitachi", "vrf_split", "cooling_generation"))
        db_session.add(_line("Engineered Comfort", "vrf_split", "cooling_generation", osp=True))
        db_session.add(_line("ClimaCool", "chillers_cooling", "cooling_generation", osp=False))
        db_session.commit()
        names = {l.name for l in eligible_lines_for_equipment_class(db_session, EquipmentClass.split_dx)}
        assert names == {"LG", "ClimateMaster", "Islandaire", "Hitachi", "Engineered Comfort"}
        assert "ClimaCool" not in names

    def test_vrf_gets_the_same_lines_as_split_dx(self, db_session):
        db_session.add(_line("LG", "vrf_split", "cooling_generation"))
        db_session.commit()
        names = {l.name for l in eligible_lines_for_equipment_class(db_session, EquipmentClass.vrf)}
        assert names == {"LG"}

    def test_cooling_tower_returns_marley_not_recold(self, db_session):
        db_session.add(_line("Marley", "cooling_towers", "heat_rejection", osp=True))
        db_session.add(_line("Recold", "cooling_towers", "heat_rejection"))  # same role, not the named line
        db_session.commit()
        names = {l.name for l in eligible_lines_for_equipment_class(db_session, EquipmentClass.cooling_tower)}
        assert names == {"Marley"}

    def test_chiller_returns_climacool_when_osp_not_required(self, db_session):
        db_session.add(_line("ClimaCool", "chillers_cooling", "cooling_generation", osp=False))
        db_session.commit()
        names = {l.name for l in eligible_lines_for_equipment_class(db_session, EquipmentClass.chiller)}
        assert names == {"ClimaCool"}

    def test_chiller_excludes_climacool_when_osp_required(self, db_session):
        """"chiller -> ClimaCool only where OSP is not required" --
        ClimaCool's own oshpd_osp=False makes this fall out of the
        existing OSP filter with no special case."""
        db_session.add(_line("ClimaCool", "chillers_cooling", "cooling_generation", osp=False))
        db_session.commit()
        assert eligible_lines_for_equipment_class(db_session, EquipmentClass.chiller, osp_required=True) == []

    def test_osp_required_includes_a_named_line_with_a_current_osp(self, db_session):
        db_session.add(_line("AAON", "rooftop_units", "air_handling", osp=True))
        db_session.commit()
        lines = eligible_lines_for_equipment_class(db_session, EquipmentClass.rooftop_packaged, osp_required=True)
        assert [l.name for l in lines] == ["AAON"]


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
