from pydantic import Field

from skrift.forms import FormModel


class ComposeTweetForm(FormModel):
    content: str = Field(
        min_length=1,
        max_length=280,
        json_schema_extra={
            "widget": "textarea",
            "label": "What's happening?",
            "placeholder": "What's happening?",
            "attrs": {"rows": "3"},
        },
    )


class ReplyForm(FormModel):
    content: str = Field(
        min_length=1,
        max_length=280,
        json_schema_extra={
            "widget": "textarea",
            "label": "Post your reply",
            "placeholder": "Post your reply",
            "attrs": {"rows": "2"},
        },
    )


class SearchForm(FormModel, form_method="get"):
    q: str = Field(
        min_length=1,
        max_length=200,
        json_schema_extra={
            "label": "Search",
            "placeholder": "Search tweets...",
        },
    )
