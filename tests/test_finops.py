from __future__ import annotations

from reposplit.utils.finops import calculate_finops


def test_calculate_finops_benchmark_values() -> None:
    # shop_monolith benchmark: ~402 LOC, 4 services, 9 routes, 4 databases
    report = calculate_finops(total_loc=402, service_count=4, route_count=9, db_count=4)
    assert report.currency == "USD"
    assert report.monolith_baseline.total_monthly_usd > report.modernized_fleet.total_monthly_usd
    assert report.monthly_savings_usd > 0
    assert report.annual_savings_usd == round(report.monthly_savings_usd * 12.0, 2)
    assert 50.0 <= report.savings_percent <= 80.0
    assert report.carbon_reduction_kg_yr > 0
    assert report.roi_multiple > 1.5
    assert len(report.assumptions) >= 4


def test_calculate_finops_scales_with_loc() -> None:
    small = calculate_finops(total_loc=500, service_count=3, route_count=6)
    large = calculate_finops(total_loc=10000, service_count=10, route_count=50)
    assert large.monolith_baseline.total_monthly_usd > small.monolith_baseline.total_monthly_usd
    assert large.annual_savings_usd > small.annual_savings_usd
