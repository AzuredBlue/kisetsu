import unittest
from unittest.mock import MagicMock, patch

import qbittorrentapi
import requests

from qbit_seasonal_anime.clients.qbit import (
    QBitClient,
    QbitAuthenticationError,
    QbitClientError,
    QbitConnectionError,
)


def _http_error(status_code):
    """Build a requests-style error carrying an HTTP status, as qbittorrent-api raises."""
    response = MagicMock()
    response.status_code = status_code
    error = requests.exceptions.HTTPError(f"{status_code} error")
    error.response = response
    return error


class TestGetClient(unittest.TestCase):
    def setUp(self):
        self.client = QBitClient(host="http://qbit:8080", username="admin", password="pw", timeout=1)

    def test_login_failure_raises_authentication_error_without_retrying(self):
        """A rejected password will not fix itself, so it must not be retried."""
        with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client") as client_cls:
            client_cls.return_value.auth_log_in.side_effect = qbittorrentapi.LoginFailed("bad creds")

            with self.assertRaises(QbitAuthenticationError):
                self.client.get_client()

        self.assertEqual(client_cls.call_count, 1)
        self.assertIsNone(self.client._client)

    def test_http_403_login_rejection_is_treated_as_authentication_failure(self):
        """qbittorrent-api reports a bad password as Forbidden403Error, not LoginFailed."""
        forbidden = _http_error(403)
        with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client") as client_cls:
            client_cls.return_value.auth_log_in.side_effect = forbidden

            with self.assertRaises(QbitAuthenticationError):
                self.client.get_client()

        self.assertEqual(client_cls.call_count, 1)
        self.assertIsNone(self.client._client)

    def test_http_401_login_rejection_is_treated_as_authentication_failure(self):
        with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client") as client_cls:
            client_cls.return_value.auth_log_in.side_effect = _http_error(401)

            with self.assertRaises(QbitAuthenticationError):
                self.client.get_client()

        self.assertEqual(client_cls.call_count, 1)

    def test_server_error_is_still_retried_as_a_connection_problem(self):
        """A 5xx is qBittorrent being unhealthy, not a credentials problem."""
        with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client") as client_cls:
            client_cls.return_value.auth_log_in.side_effect = _http_error(503)
            with patch("qbit_seasonal_anime.clients.qbit.time.sleep"):
                with self.assertRaises(QbitConnectionError):
                    self.client.get_client()

        self.assertEqual(client_cls.call_count, 3)

    def test_exhausted_retries_raise_connection_error(self):
        with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client") as client_cls:
            client_cls.return_value.auth_log_in.side_effect = ConnectionRefusedError("refused")
            with patch("qbit_seasonal_anime.clients.qbit.time.sleep"):
                with self.assertRaises(QbitConnectionError) as ctx:
                    self.client.get_client()

        self.assertIn("Cannot connect to qBittorrent at http://qbit:8080", str(ctx.exception))
        self.assertIn("after 3 attempts", str(ctx.exception))
        self.assertIsNone(self.client._client)

    def test_default_retries_wait_longer_than_a_single_attempt(self):
        """The default backoff must span a container's WebUI startup window."""
        with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client") as client_cls:
            client_cls.return_value.auth_log_in.side_effect = ConnectionRefusedError("refused")
            with patch("qbit_seasonal_anime.clients.qbit.time.sleep") as sleep:
                with self.assertRaises(QbitConnectionError):
                    self.client.get_client()

        self.assertEqual(client_cls.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0])

    def test_max_retries_keyword_remains_supported(self):
        with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client") as client_cls:
            client_cls.return_value.auth_log_in.side_effect = ConnectionRefusedError("refused")
            with patch("qbit_seasonal_anime.clients.qbit.time.sleep"):
                with self.assertRaises(QbitConnectionError) as ctx:
                    self.client.get_client(max_retries=1)

        self.assertEqual(client_cls.call_count, 1)
        self.assertIn("after 1 attempts", str(ctx.exception))

    def test_successful_login_is_memoized(self):
        with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client") as client_cls:
            first = self.client.get_client()
            second = self.client.get_client()

        self.assertIs(first, second)
        self.assertEqual(client_cls.call_count, 1)


class TestTestConnection(unittest.TestCase):
    def setUp(self):
        self.client = QBitClient(host="http://qbit:8080", username="admin", password="pw", timeout=1)

    def test_preserves_authentication_subtype(self):
        with patch.object(self.client, "get_client", side_effect=QbitAuthenticationError("login failed")):
            with self.assertRaises(QbitAuthenticationError):
                self.client.test_connection()

    def test_preserves_connection_subtype(self):
        with patch.object(self.client, "get_client", side_effect=QbitConnectionError("refused")):
            with self.assertRaises(QbitConnectionError):
                self.client.test_connection()

    def test_wraps_unexpected_errors_as_connection_error(self):
        api = MagicMock()
        type(api).app = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))

        with patch.object(self.client, "get_client", return_value=api):
            with self.assertRaises(QbitConnectionError) as ctx:
                self.client.test_connection()

        self.assertIn("Failed to query qBittorrent version", str(ctx.exception))

    def test_returns_versions_on_success(self):
        api = MagicMock()
        api.app.version = "v4.6.0"
        api.app.web_api_version = "2.9.3"

        with patch.object(self.client, "get_client", return_value=api):
            result = self.client.test_connection()

        self.assertEqual(result, {"app_version": "v4.6.0", "api_version": "2.9.3"})


class TestSetRssRule(unittest.TestCase):
    def setUp(self):
        self.client = QBitClient(host="http://qbit:8080", username="admin", password="pw", timeout=1)

    def test_propagates_connection_error_when_session_cannot_be_opened(self):
        with patch.object(self.client, "get_client", side_effect=QbitConnectionError("refused")):
            with self.assertRaises(QbitConnectionError):
                self.client.set_rss_rule(rule_name="r", rule_def={})

    def test_preserves_connection_subtype_when_the_call_itself_fails(self):
        """A reset mid-call must be reported as unreachable, not as a bad rule."""
        api = MagicMock()
        api.rss_set_rule.side_effect = ConnectionResetError(104, "reset by peer")

        with patch.object(self.client, "get_client", side_effect=[api, QbitConnectionError("refused")]):
            with self.assertRaises(QbitConnectionError):
                self.client.set_rss_rule(rule_name="r", rule_def={})

        self.assertIsNone(self.client._client)

    def test_wraps_rule_rejection_as_client_error(self):
        api = MagicMock()
        api.rss_set_rule.side_effect = ValueError("bad regex")

        with patch.object(self.client, "get_client", side_effect=[api, api]):
            with self.assertRaises(QbitClientError) as ctx:
                self.client.set_rss_rule(rule_name="r", rule_def={})

        self.assertNotIsInstance(ctx.exception, QbitConnectionError)


if __name__ == "__main__":
    unittest.main()
