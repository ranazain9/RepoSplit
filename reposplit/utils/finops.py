"""FinOps Cloud Cost & ROI Calculation Engine.

Calculates deterministic infrastructure spend comparisons between a monolithic deployment
(provisioned peak vertical instances) and an elastic modernized microservices fleet
(horizontal autoscaling containers on Kubernetes / Red Hat OpenShift).
"""

from __future__ import annotations

from reposplit.core.schemas import CostBreakdown, FinOpsReport


def calculate_finops(
    total_loc: int,
    service_count: int,
    route_count: int,
    db_count: int = 1,
) -> FinOpsReport:
    """Calculate cloud infrastructure economics and carbon reduction."""
    # Monolith: Peak-provisioned vertical compute + monolithic DB instance + load balancer
    loc_factor = max(1.0, total_loc / 400.0)
    mono_compute = round(min(1800.0, 150.0 * (1.0 + (loc_factor - 1.0) * 0.4)), 2)
    mono_db = round(min(800.0, 85.0 * (1.0 + (loc_factor - 1.0) * 0.3)), 2)
    mono_gw = 35.0  # Application Load Balancer base
    mono_total = round(mono_compute + mono_db + mono_gw, 2)

    monolith_breakdown = CostBreakdown(
        compute_monthly_usd=mono_compute,
        database_monthly_usd=mono_db,
        ingress_gateway_monthly_usd=mono_gw,
        total_monthly_usd=mono_total,
    )

    # Modernized Fleet: Elastic autoscaled container pods + partitioned storage + Envoy gateway
    micro_compute_per_svc = 12.50  # 0.25 vCPU baseline request + traffic burst headroom
    micro_compute = round(service_count * micro_compute_per_svc + (route_count * 0.75), 2)
    micro_db = round(db_count * 9.50, 2)  # Partitioned serverless/isolated storage tiers
    micro_gw = 18.0  # Shared Envoy proxy gateway pod footprint
    micro_total = round(micro_compute + micro_db + micro_gw, 2)

    micro_breakdown = CostBreakdown(
        compute_monthly_usd=micro_compute,
        database_monthly_usd=micro_db,
        ingress_gateway_monthly_usd=micro_gw,
        total_monthly_usd=micro_total,
    )

    monthly_savings = max(0.0, round(mono_total - micro_total, 2))
    annual_savings = round(monthly_savings * 12.0, 2)
    savings_percent = round((monthly_savings / mono_total) * 100.0, 1) if mono_total > 0 else 0.0

    # Carbon reduction: ~0.385 kg CO2e per kWh saved (standard US/EU datacenter average)
    kwh_saved_monthly = monthly_savings * 2.8
    carbon_reduction_kg_yr = round(kwh_saved_monthly * 12.0 * 0.385, 1)

    roi_multiple = round(mono_total / micro_total, 2) if micro_total > 0 else 1.0

    assumptions = [
        f"Monolith provisioned for peak load: ${mono_compute:.2f}/mo compute + ${mono_db:.2f}/mo DB",
        f"Microservices fleet: {service_count} elastic container services (${micro_compute:.2f}/mo compute)",
        "Ingress: Envoy Strangler Gateway proxy ($18.00/mo) vs legacy ALB ($35.00/mo)",
        "Carbon intensity factor: 0.385 kg CO2e / kWh grid average",
    ]

    return FinOpsReport(
        currency="USD",
        monolith_baseline=monolith_breakdown,
        modernized_fleet=micro_breakdown,
        monthly_savings_usd=monthly_savings,
        annual_savings_usd=annual_savings,
        savings_percent=savings_percent,
        carbon_reduction_kg_yr=carbon_reduction_kg_yr,
        roi_multiple=roi_multiple,
        assumptions=assumptions,
    )
