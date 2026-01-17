from advanced_alchemy.base import UUIDAuditBase


class Base(UUIDAuditBase):
    """Base model class with UUID primary key and audit timestamps."""

    __abstract__ = True
