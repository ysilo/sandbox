"""src.dashboards — panneau HTML, CostRepository, FastAPI (§14)."""
from src.dashboards.api import create_app
from src.dashboards.cost_repo import (
    AgentCostRow,
    ApiSourceRow,
    ConsumerRow,
    CostAlert,
    CostPanel,
    CostRepository,
    DailyCostPoint,
    ModelCostRow,
)
from src.dashboards.html_builder import DASHBOARDS_DIR, HTMLDashboardBuilder
from src.dashboards.pricing import LLMLimits, ModelPricing

__all__ = [
    "create_app",
    "CostRepository",
    "CostPanel",
    "AgentCostRow",
    "ModelCostRow",
    "ApiSourceRow",
    "DailyCostPoint",
    "ConsumerRow",
    "CostAlert",
    "HTMLDashboardBuilder",
    "DASHBOARDS_DIR",
    "ModelPricing",
    "LLMLimits",
]
