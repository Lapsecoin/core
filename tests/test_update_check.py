"""
Unit tests for update_check.py: version parsing/classification and the
UpdateChecker's single-check logic (network mocked, no real requests).
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from update_check import UpdateChecker, _parse_version, classify_update


class TestParseVersion:
    def test_parses_dotted_integers(self):
        assert _parse_version("0.1.1") == (0, 1, 1)

    def test_strips_whitespace(self):
        assert _parse_version("  1.2.3\n") == (1, 2, 3)

    def test_none_for_non_numeric(self):
        assert _parse_version("not-a-version") is None

    def test_none_for_non_string(self):
        assert _parse_version(None) is None


class TestClassifyUpdate:
    def test_none_when_not_newer(self):
        assert classify_update("0.1.1", "0.1.1") is None
        assert classify_update("0.1.0", "0.1.1") is None

    def test_none_when_unparseable(self):
        assert classify_update("garbage", "0.1.1") is None
        assert classify_update("0.1.1", "garbage") is None

    def test_patch_bump_is_minor(self):
        assert classify_update("0.1.2", "0.1.1") == "minor"

    def test_second_component_bump_is_critical(self):
        assert classify_update("0.2.0", "0.1.1") == "critical"

    def test_first_component_bump_is_protocol(self):
        assert classify_update("1.0.0", "0.1.1") == "protocol"

    def test_first_component_change_wins_even_if_others_also_differ(self):
        # Major changed *and* minor/patch changed, protocol still wins,
        # since that's the most significant differing component.
        assert classify_update("2.5.9", "0.1.1") == "protocol"

    def test_handles_differing_component_counts(self):
        assert classify_update("0.2", "0.1.1") == "critical"
        assert classify_update("1", "0.9.9") == "protocol"


class TestUpdateChecker:
    def test_start_noop_when_disabled(self):
        checker = UpdateChecker(local_version="0.1.1", version_url="")
        with patch("threading.Thread") as thread_cls:
            checker.start()
        thread_cls.assert_not_called()

    def test_check_once_sets_severity_on_newer_version(self):
        checker = UpdateChecker(local_version="0.1.1",
                                version_url="http://example.invalid/VERSION")
        fake_response = MagicMock(status_code=200, text="0.2.0\n")
        fake_requests = MagicMock()
        fake_requests.get.return_value = fake_response
        with patch.dict(sys.modules, {"requests": fake_requests}):
            assert checker.check_once() is True
        assert checker.severity == "critical"
        assert checker.latest_version == "0.2.0"

    def test_check_once_leaves_state_when_not_newer(self):
        checker = UpdateChecker(local_version="0.1.1",
                                version_url="http://example.invalid/VERSION")
        fake_response = MagicMock(status_code=200, text="0.1.1")
        fake_requests = MagicMock()
        fake_requests.get.return_value = fake_response
        with patch.dict(sys.modules, {"requests": fake_requests}):
            assert checker.check_once() is True       # read fine, just nothing newer
        assert checker.severity is None
        assert checker.latest_version is None

    def test_check_once_survives_non_200_response(self):
        checker = UpdateChecker(local_version="0.1.1",
                                version_url="http://example.invalid/VERSION")
        fake_response = MagicMock(status_code=404, text="")
        fake_requests = MagicMock()
        fake_requests.get.return_value = fake_response
        with patch.dict(sys.modules, {"requests": fake_requests}):
            assert checker.check_once() is False  # must not raise
        assert checker.severity is None

    def test_check_once_survives_network_exception(self):
        checker = UpdateChecker(local_version="0.1.1",
                                version_url="http://example.invalid/VERSION")
        fake_requests = MagicMock()
        fake_requests.get.side_effect = OSError("network down")
        with patch.dict(sys.modules, {"requests": fake_requests}):
            checker.check_once()  # must not raise
        assert checker.severity is None

    def test_check_once_survives_missing_requests(self):
        checker = UpdateChecker(local_version="0.1.1",
                                version_url="http://example.invalid/VERSION")
        with patch.dict(sys.modules, {"requests": None}):
            checker.check_once()  # must not raise
        assert checker.severity is None


class TestNoticeText:
    """The one line of the desktop window, so a user can tell "up to date"
    from "could not check" and is sent to the node page to update."""

    def test_nothing_until_there_is_something_to_say(self):
        from update_check import notice_text
        assert notice_text(None, None) == ""

    def test_an_available_update_names_the_version_and_where_to_update(self):
        from update_check import notice_text
        assert notice_text("minor", "1.2.3") == "v1.2.3 is available. Open Node to update."
        assert notice_text("minor", "1.2.3", check_ok=True).startswith("v1.2.3 is available")

    def test_a_check_that_found_nothing_says_up_to_date(self):
        from update_check import notice_text
        assert notice_text(None, None, check_ok=True) == "Up to date."

    def test_a_check_that_failed_says_so(self):
        from update_check import notice_text
        assert notice_text(None, None, check_ok=False) == "Could not check for updates."
