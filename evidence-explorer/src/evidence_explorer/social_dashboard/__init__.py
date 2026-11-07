"""Read-only multidimensional projections of saved investigation evidence."""

from .contracts import DashboardFilters, DashboardProjection
from .projection import project_dashboard

__all__ = ["DashboardFilters", "DashboardProjection", "project_dashboard"]
