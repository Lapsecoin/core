"""The check that a peer's web port answers (http_probe)."""

from unittest import mock

import http_probe


class TestProbe:
    def _answer(self, body, status=200):
        resp = mock.MagicMock()
        resp.status = status
        resp.read.return_value = body
        resp.__enter__.return_value = resp
        return mock.patch("urllib.request.urlopen", return_value=resp)

    def test_an_answering_peer_is_reachable(self):
        with self._answer(b'{"height": 5}'):
            assert http_probe._probe_one("1.2.3.4:8333", 1) is True

    def test_a_reply_that_is_not_json_still_counts_as_answering(self):
        with self._answer(b"<html>"):
            assert http_probe._probe_one("1.2.3.4:8333", 1) is True

    def test_our_own_instance_is_recognised(self):
        with self._answer(b'{"iid": "me"}'):
            assert http_probe._probe_one("1.2.3.4:8333", 1, own_iid="me") is None

    def test_another_instance_is_not_us(self):
        with self._answer(b'{"iid": "them"}'):
            assert http_probe._probe_one("1.2.3.4:8333", 1, own_iid="me") is True

    def test_an_error_is_not_reachable(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            assert http_probe._probe_one("1.2.3.4:8333", 1) is False

    def test_a_bad_status_is_not_reachable(self):
        with self._answer(b"{}", status=503):
            assert http_probe._probe_one("1.2.3.4:8333", 1) is False
