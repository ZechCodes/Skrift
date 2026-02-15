"""Admin module for administrative functionality."""

from skrift.admin.controller import (
    AdminController,
    PageAdminController,
    UserAdminController,
    SettingsAdminController,
)
from skrift.admin.navigation import AdminNavItem, build_admin_nav, ADMIN_NAV_TAG

__all__ = [
    "AdminController",
    "PageAdminController",
    "UserAdminController",
    "SettingsAdminController",
    "AdminNavItem",
    "build_admin_nav",
    "ADMIN_NAV_TAG",
]
