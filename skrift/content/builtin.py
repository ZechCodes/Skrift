"""Built-in content areas shipped with Skrift.

``HomeContent`` powers the default landing page template (``index.html``).
Importing this module registers the areas, so it is imported from
:mod:`skrift.content` to guarantee they are always available to the admin and
the public renderer.
"""

from __future__ import annotations

from skrift.content.fields import group, repeater, text, textarea, url
from skrift.content.schema import ContentArea, ContentModel


class CallToAction(ContentModel):
    """A button label paired with its destination link."""

    label: str = text("Button label", default="Get started")
    url: str = url("Button link", default="/auth/login", placeholder="/signup or https://…")


class HomeSection(ContentModel):
    """A heading-and-body block stacked below the hero."""

    heading: str = text("Heading", default="")
    body: str = textarea("Body", default="", rows=4)


class HomeContent(
    ContentArea,
    key="home",
    label="Home Page",
    description="Hero and content sections shown on the site landing page.",
):
    """The editable content of the default landing page."""

    hero_title: str = text("Hero title", default="Welcome to your new site")
    hero_subtitle: str = textarea(
        "Hero subtitle",
        default="Publish content, manage pages, and make it your own.",
        rows=3,
    )
    cta: CallToAction = group(CallToAction, label="Call to action")
    sections: list[HomeSection] = repeater(
        HomeSection, label="Sections", item_label="Section"
    )
