"""FinOps Cloud Cost & ROI Calculation Engine.

Calculates deterministic infrastructure spend comparisons between a monolithic deployment
(provisioned peak vertical instances) and an elastic modernized microservices fleet
(horizontal autoscaling containers on Kubernetes / Red Hat OpenShift).
"""

from __future__ import annotations

from reposplit.core.schemas import CostBreakdown, FinOpsReport

# --------------------------------------------------------------------------------------
# Industry Benchmark Constants & Provenance Citations
# --------------------------------------------------------------------------------------
# 1. Monolith baseline compute: AWS EC2 t3.xlarge / c5.xlarge (4 vCPU, 16 GiB peak fixed VM)
#    Source: AWS On-Demand Pricing (us-east-1), ~$146-153/mo.
MONOLITH_BASE_COMPUTE_USD = 150.00

# 2. Monolith database: AWS RDS db.m5.large (2 vCPU, 8 GiB RAM, 100GB gp3)
#    Source: AWS RDS PostgreSQL Pricing, ~$85.00/mo.
MONOLITH_BASE_DB_USD = 85.00

# 3. Application Load Balancer / Ingress baseline:
#    Source: AWS Application Load Balancer fixed base ($0.0225/hr + LCU base), ~$35.00/mo.
MONOLITH_BASE_GATEWAY_USD = 35.00

# 4. Modernized fleet: Container pods on Kubernetes / Red Hat OpenShift
#    Baseline: 0.25 vCPU, 512 MiB request + horizontal pod autoscaling (HPA)
#    Source: Kubernetes / EKS Fargate average request tier ($0.04048/vCPU-hr), ~$12.50/mo per service.
CONTAINER_SVC_MONTHLY_USD = 12.50
ROUTE_COMPUTE_MONTHLY_USD = 0.75

# 5. Partitioned microservice DB storage tiers:
#    Source: AWS Aurora Serverless v2 min ACU / Cloud SQL minimal tier, ~$9.50/mo per DB.
MICROSERVICE_DB_MONTHLY_USD = 9.50

# 6. Shared Envoy Proxy / Kong ingress controller pod footprint:
#    Source: Envoy lightweight sidecar / ingress deployment, ~$18.00/mo.
ENVOY_GATEWAY_MONTHLY_USD = 18.00

# 7. Datacenter Carbon & Energy Emissions Factors:
#    Source: US EPA eGRID & European Environment Agency (EEA) Cloud Datacenter averages:
#    ~2.8 kWh per $ of compute expenditure; ~0.385 kg CO2e per kWh saved.
KWH_PER_USD_SPENT = 2.8
CO2E_KG_PER_KWH = 0.385


def calculate_finops(
    total_loc: int,
    service_count: int,
    route_count: int,
    db_count: int = 1,
) -> FinOpsReport:
    """Calculate cloud infrastructure economics and carbon reduction."""
    # Monolith: Peak-provisioned vertical compute + monolithic DB instance + load balancer
    loc_factor = max(1.0, total_loc / 400.0)
    mono_compute = round(min(1800.0, MONOLITH_BASE_COMPUTE_USD * (1.0 + (loc_factor - 1.0) * 0.4)), 2)
    mono_db = round(min(800.0, MONOLITH_BASE_DB_USD * (1.0 + (loc_factor - 1.0) * 0.3)), 2)
    mono_gw = MONOLITH_BASE_GATEWAY_USD
    mono_total = round(mono_compute + mono_db + mono_gw, 2)

    monolith_breakdown = CostBreakdown(
        compute_monthly_usd=mono_compute,
        database_monthly_usd=mono_db,
        ingress_gateway_monthly_usd=mono_gw,
        total_monthly_usd=mono_total,
    )

    # Modernized Fleet: Elastic autoscaled container pods + partitioned storage + Envoy gateway
    micro_compute = round(service_count * CONTAINER_SVC_MONTHLY_USD + (route_count * ROUTE_COMPUTE_MONTHLY_USD), 2)
    micro_db = round(db_count * MICROSERVICE_DB_MONTHLY_USD, 2)
    micro_gw = ENVOY_GATEWAY_MONTHLY_USD
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

    kwh_saved_monthly = monthly_savings * KWH_PER_USD_SPENT
    carbon_reduction_kg_yr = round(kwh_saved_monthly * 12.0 * CO2E_KG_PER_KWH, 1)

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
