from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from skrift.db.base import Base


class ContentAreaRecord(Base):
    """Stored values for a code-declared content area, keyed by its schema key.

    The schema (fields, labels, widgets) lives in code; this table holds only
    the editor-supplied values as a JSON document validated on save.
    """

    __tablename__ = "content_areas"

    key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
