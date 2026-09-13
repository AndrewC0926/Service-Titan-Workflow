"""Block 4B Item 1: server-rendered SVG charts (app.web.charts)."""
from app.web.charts import bar_chart_svg, sparkline_svg


class TestSparklineSvg:
    def test_draws_a_path_through_real_points(self):
        svg = sparkline_svg([1, 2, 3, 2, 1])
        assert "<path" in svg
        assert "<circle" in svg

    def test_fewer_than_two_points_draws_a_dashed_flat_line_not_a_crash(self):
        svg = sparkline_svg([5])
        assert "stroke-dasharray" in svg
        assert "not enough history" in svg

    def test_empty_list_does_not_crash(self):
        svg = sparkline_svg([])
        assert "stroke-dasharray" in svg

    def test_none_values_are_dropped_not_interpolated(self):
        svg = sparkline_svg([1, None, None, 4])
        assert "<path" in svg
        assert "aria-label=\"trend over 2 periods\"" in svg

    def test_all_none_falls_back_to_flat_line(self):
        svg = sparkline_svg([None, None])
        assert "not enough history" in svg

    def test_flat_series_does_not_divide_by_zero(self):
        svg = sparkline_svg([3, 3, 3])
        assert "<path" in svg


class TestBarChartSvg:
    def test_renders_one_bar_per_item(self):
        svg = bar_chart_svg([("AB 869", 10), ("SB 1206", 20)])
        assert svg.count("<rect") == 2

    def test_empty_items_renders_nothing(self):
        assert bar_chart_svg([]) == ""

    def test_action_label_gets_the_action_class(self):
        svg = bar_chart_svg([("Overdue", 5), ("On track", 10)], action_labels={"Overdue"})
        assert 'class="bar action"' in svg
        assert svg.count('class="bar"') == 1

    def test_labels_are_html_escaped(self):
        svg = bar_chart_svg([("<script>alert(1)</script>", 1)])
        assert "<script>" not in svg
        assert "&lt;script&gt;" in svg

    def test_zero_max_value_does_not_divide_by_zero(self):
        svg = bar_chart_svg([("Empty", 0), ("Also empty", 0)])
        assert svg.count("<rect") == 2
