"""Association table linking pages to assets (many-to-many)."""

from sqlalchemy import Column, ForeignKey, Table

from skrift.db.base import Base

page_assets = Table(
    "page_assets",
    Base.metadata,
    Column("page_id", ForeignKey("pages.id", ondelete="CASCADE"), primary_key=True),
    Column("asset_id", ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True),
)
