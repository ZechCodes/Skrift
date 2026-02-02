"""Enhanced flash message system with types and multiple messages support."""

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from litestar import Request


class FlashType(str, Enum):
    """Types of flash messages with corresponding CSS classes."""

    SUCCESS = "success"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class FlashMessage:
    """A flash message with type and dismissibility."""

    message: str
    type: FlashType = FlashType.INFO
    dismissible: bool = True


def add_flash(
    request: "Request",
    message: str,
    flash_type: FlashType = FlashType.INFO,
    dismissible: bool = True,
) -> None:
    """Add a flash message to the session queue.

    Args:
        request: The Litestar request object
        message: The message text to display
        flash_type: Type of message (success, error, warning, info)
        dismissible: Whether the message can be dismissed by the user
    """
    if "flash_messages" not in request.session:
        request.session["flash_messages"] = []

    request.session["flash_messages"].append({
        "message": message,
        "type": flash_type.value,
        "dismissible": dismissible,
    })


def get_flash_messages(request: "Request") -> list[FlashMessage]:
    """Get and clear all flash messages from the session.

    Also handles backwards compatibility with old single-string flash.

    Args:
        request: The Litestar request object

    Returns:
        List of FlashMessage objects
    """
    messages = request.session.pop("flash_messages", [])

    # Backwards compatibility: convert old single-string flash
    old_flash = request.session.pop("flash", None)
    if old_flash:
        messages.insert(0, {
            "message": old_flash,
            "type": FlashType.INFO.value,
            "dismissible": True,
        })

    return [
        FlashMessage(
            message=m["message"],
            type=FlashType(m["type"]),
            dismissible=m.get("dismissible", True),
        )
        for m in messages
    ]


# Convenience functions for common flash types
def flash_success(request: "Request", message: str, dismissible: bool = True) -> None:
    """Add a success flash message."""
    add_flash(request, message, FlashType.SUCCESS, dismissible)


def flash_error(request: "Request", message: str, dismissible: bool = True) -> None:
    """Add an error flash message."""
    add_flash(request, message, FlashType.ERROR, dismissible)


def flash_warning(request: "Request", message: str, dismissible: bool = True) -> None:
    """Add a warning flash message."""
    add_flash(request, message, FlashType.WARNING, dismissible)


def flash_info(request: "Request", message: str, dismissible: bool = True) -> None:
    """Add an info flash message."""
    add_flash(request, message, FlashType.INFO, dismissible)
