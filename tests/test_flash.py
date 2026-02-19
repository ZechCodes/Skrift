"""Tests for the enhanced flash message system."""

import pytest

from skrift.lib.flash import (
    FlashType,
    FlashMessage,
    add_flash,
    get_flash_messages,
    flash_success,
    flash_error,
    flash_warning,
    flash_info,
)


@pytest.fixture
def mock_request(mock_request_factory):
    """Create a mock request with a session dict."""
    return mock_request_factory()


class TestFlashType:
    """Test the FlashType enum."""

    def test_flash_type_values(self):
        """Test that FlashType has expected values."""
        assert FlashType.SUCCESS.value == "success"
        assert FlashType.ERROR.value == "error"
        assert FlashType.WARNING.value == "warning"
        assert FlashType.INFO.value == "info"


class TestFlashMessage:
    """Test the FlashMessage dataclass."""

    def test_flash_message_defaults(self):
        """Test FlashMessage default values."""
        msg = FlashMessage(message="Test")
        assert msg.message == "Test"
        assert msg.type == FlashType.INFO
        assert msg.dismissible is True

    def test_flash_message_custom_values(self):
        """Test FlashMessage with custom values."""
        msg = FlashMessage(
            message="Error occurred",
            type=FlashType.ERROR,
            dismissible=False,
        )
        assert msg.message == "Error occurred"
        assert msg.type == FlashType.ERROR
        assert msg.dismissible is False


class TestAddFlash:
    """Test the add_flash function."""

    def test_add_flash_creates_list(self, mock_request):
        """Test that add_flash creates flash_messages list if not exists."""
        add_flash(mock_request, "Test message")
        assert "flash_messages" in mock_request.session
        assert len(mock_request.session["flash_messages"]) == 1

    def test_add_multiple_flash_messages(self, mock_request):
        """Test that multiple flash messages can be added."""
        add_flash(mock_request, "First")
        add_flash(mock_request, "Second")
        add_flash(mock_request, "Third")

        assert len(mock_request.session["flash_messages"]) == 3

    def test_add_flash_with_type(self, mock_request):
        """Test adding flash with specific type."""
        add_flash(mock_request, "Error", FlashType.ERROR)

        msg = mock_request.session["flash_messages"][0]
        assert msg["type"] == "error"

    def test_add_flash_dismissible_false(self, mock_request):
        """Test adding non-dismissible flash."""
        add_flash(mock_request, "Important", dismissible=False)

        msg = mock_request.session["flash_messages"][0]
        assert msg["dismissible"] is False


class TestGetFlashMessages:
    """Test the get_flash_messages function."""

    def test_get_flash_messages_clears_session(self, mock_request):
        """Test that get_flash_messages clears the messages."""
        add_flash(mock_request, "Test")

        messages = get_flash_messages(mock_request)

        assert len(messages) == 1
        assert "flash_messages" not in mock_request.session

    def test_get_flash_messages_returns_dataclass(self, mock_request):
        """Test that get_flash_messages returns FlashMessage objects."""
        add_flash(mock_request, "Test", FlashType.SUCCESS)

        messages = get_flash_messages(mock_request)

        assert isinstance(messages[0], FlashMessage)
        assert messages[0].message == "Test"
        assert messages[0].type == FlashType.SUCCESS

    def test_backwards_compat_old_flash_string(self, mock_request):
        """Test backwards compatibility with old single-string flash."""
        mock_request.session["flash"] = "Old style message"

        messages = get_flash_messages(mock_request)

        assert len(messages) == 1
        assert messages[0].message == "Old style message"
        assert messages[0].type == FlashType.INFO
        assert "flash" not in mock_request.session

    def test_backwards_compat_combined_with_new(self, mock_request):
        """Test that old flash is combined with new messages."""
        mock_request.session["flash"] = "Old message"
        add_flash(mock_request, "New message", FlashType.SUCCESS)

        messages = get_flash_messages(mock_request)

        # Old message should be first (inserted at position 0)
        assert len(messages) == 2
        assert messages[0].message == "Old message"
        assert messages[1].message == "New message"

    def test_empty_session_returns_empty_list(self, mock_request):
        """Test that empty session returns empty list."""
        messages = get_flash_messages(mock_request)
        assert messages == []


class TestConvenienceFunctions:
    """Test the convenience flash functions."""

    def test_flash_success_sets_type(self, mock_request):
        """Test flash_success sets correct type."""
        flash_success(mock_request, "Success!")

        msg = mock_request.session["flash_messages"][0]
        assert msg["type"] == "success"

    def test_flash_error_sets_type(self, mock_request):
        """Test flash_error sets correct type."""
        flash_error(mock_request, "Error!")

        msg = mock_request.session["flash_messages"][0]
        assert msg["type"] == "error"

    def test_flash_warning_sets_type(self, mock_request):
        """Test flash_warning sets correct type."""
        flash_warning(mock_request, "Warning!")

        msg = mock_request.session["flash_messages"][0]
        assert msg["type"] == "warning"

    def test_flash_info_sets_type(self, mock_request):
        """Test flash_info sets correct type."""
        flash_info(mock_request, "Info!")

        msg = mock_request.session["flash_messages"][0]
        assert msg["type"] == "info"

    def test_convenience_functions_dismissible_param(self, mock_request):
        """Test that convenience functions accept dismissible parameter."""
        flash_success(mock_request, "Test", dismissible=False)

        msg = mock_request.session["flash_messages"][0]
        assert msg["dismissible"] is False
