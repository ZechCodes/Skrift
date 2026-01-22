"""Admin module for administrative functionality."""

from skrift.admin.controller import AdminController
from skrift.admin.navigation import AdminNavItem, build_admin_nav, ADMIN_NAV_TAG

__all__ = [
    "AdminController",
    "AdminNavItem",
    "build_admin_nav",
    "ADMIN_NAV_TAG",
]
