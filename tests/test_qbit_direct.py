from unittest.mock import MagicMock, patch

import pytest
import qbittorrentapi

from qbit_seasonal_anime.clients.qbit import QBitClient, QbitAuthenticationError, QbitClientError, QbitConnectionError


def _client():
    client = QBitClient(host="http://localhost:8080")
    underlying = MagicMock()
    client.get_client = MagicMock(return_value=underlying)
    return client, underlying


def test_add_torrent_passes_direct_ownership_options():
    client, underlying = _client()
    underlying.torrents_add.return_value = "Ok."

    assert client.add_torrent(
        urls="magnet:test",
        save_path="/anime",
        category="Anime",
        tags=["qsa-managed", "qsa-op-1"],
        is_paused=True,
        ratio_limit=1.5,
    )

    underlying.torrents_add.assert_called_once_with(
        urls="magnet:test",
        save_path="/anime",
        category="Anime",
        tags="qsa-managed,qsa-op-1",
        is_paused=True,
        ratio_limit=1.5,
        use_auto_torrent_management=False,
        share_limit_action="Stop",
    )


def test_add_torrent_stops_once_the_share_limit_is_reached():
    client, underlying = _client()
    underlying.torrents_add.return_value = "Ok."

    assert client.add_torrent(urls="magnet:test", ratio_limit=1.0)
    assert underlying.torrents_add.call_args.kwargs["share_limit_action"] == "Stop"


def test_add_torrent_can_keep_the_inherited_share_limit_action():
    client, underlying = _client()
    underlying.torrents_add.return_value = "Ok."

    client.add_torrent(urls="magnet:test", share_limit_action=None)
    assert "share_limit_action" not in underlying.torrents_add.call_args.kwargs


def test_stop_torrents_is_forwarded():
    client, underlying = _client()

    client.stop_torrents(["a", "b"])
    underlying.torrents_stop.assert_called_once_with(torrent_hashes=["a", "b"])

    client.stop_torrents([])
    underlying.torrents_stop.assert_called_once()


def test_add_torrent_reports_a_plain_failure_response():
    client, underlying = _client()
    # qBittorrent answers some rejections with a "Fails." body and no HTTP error.
    underlying.torrents_add.return_value = "Fails."

    with pytest.raises(QbitClientError):
        client.add_torrent(urls="magnet:test")


def test_add_torrent_forwards_a_zero_ratio_limit():
    client, underlying = _client()
    underlying.torrents_add.return_value = "Ok."

    assert client.add_torrent(urls="magnet:test", ratio_limit=0)
    # 0 means "seed forever" and must not be dropped.
    assert underlying.torrents_add.call_args.kwargs["ratio_limit"] == 0


def test_get_torrents_filters_by_hash_category_and_tag():
    client, underlying = _client()
    underlying.torrents_info.return_value = []

    client.get_torrents()
    assert underlying.torrents_info.call_args.kwargs == {}

    client.get_torrents(hashes=["a", "b"], category="Anime", tag="qsa-managed")
    assert underlying.torrents_info.call_args.kwargs == {
        "torrent_hashes": ["a", "b"],
        "category": "Anime",
        "tag": "qsa-managed",
    }


def test_torrent_lookup_and_lifecycle_methods_delegate_to_client():
    client, underlying = _client()
    underlying.torrents_info.return_value = [MagicMock(hash="abc")]

    torrents = client.get_torrents(tag="qsa-managed")
    client.pause_torrents(["abc"])
    client.resume_torrents(["abc"])
    client.recheck_torrents(["abc"])
    client.delete_torrents(["abc"], delete_files=True)

    assert torrents[0].hash == "abc"
    underlying.torrents_info.assert_called_once_with(tag="qsa-managed")
    underlying.torrents_pause.assert_called_once_with(torrent_hashes=["abc"])
    underlying.torrents_resume.assert_called_once_with(torrent_hashes=["abc"])
    underlying.torrents_recheck.assert_called_once_with(torrent_hashes=["abc"])
    underlying.torrents_delete.assert_called_once_with(delete_files=True, torrent_hashes=["abc"])


def test_add_torrent_rejects_qbittorrent_failure_response():
    client, underlying = _client()
    underlying.torrents_add.return_value = "Fails."

    with pytest.raises(QbitClientError):
        client.add_torrent(urls="https://invalid.example.torrent")


def test_refresh_rss_reports_request_acceptance():
    client, underlying = _client()

    assert client.refresh_rss_feeds() is True

    underlying.rss_refresh_item.assert_called_once_with(item_path="")


def test_refresh_rss_reports_request_failure():
    client, underlying = _client()
    underlying.rss_refresh_item.side_effect = RuntimeError("refresh failed")

    assert client.refresh_rss_feeds() is False


def test_get_client_retries_transient_connection_failure():
    connected_client = MagicMock()
    client_factory = MagicMock(side_effect=[ConnectionError("starting"), connected_client])
    sleep = MagicMock()

    with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client", client_factory), patch(
        "qbit_seasonal_anime.clients.qbit.time.sleep", sleep
    ):
        client = QBitClient(host="http://qbit:8080")
        result = client.get_client(max_attempts=3, backoff_factor=1.0)

    assert result is connected_client
    assert client_factory.call_count == 2
    sleep.assert_called_once_with(1.0)


def test_get_client_raises_connection_error_after_bounded_attempts():
    client_factory = MagicMock(side_effect=ConnectionError("not ready"))
    sleep = MagicMock()

    with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client", client_factory), patch(
        "qbit_seasonal_anime.clients.qbit.time.sleep", sleep
    ):
        client = QBitClient(host="http://qbit:8080")
        with pytest.raises(QbitConnectionError):
            client.get_client(max_attempts=3, backoff_factor=1.0)

    assert client_factory.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [1.0, 2.0]


def test_get_client_does_not_retry_authentication_failure():
    client_factory = MagicMock()
    client_factory.return_value.auth_log_in.side_effect = qbittorrentapi.LoginFailed("invalid credentials")
    sleep = MagicMock()

    with patch("qbit_seasonal_anime.clients.qbit.qbittorrentapi.Client", client_factory), patch(
        "qbit_seasonal_anime.clients.qbit.time.sleep", sleep
    ):
        client = QBitClient(host="http://qbit:8080")
        with pytest.raises(QbitAuthenticationError):
            client.get_client(max_attempts=3, backoff_factor=1.0)

    assert client_factory.call_count == 1
    sleep.assert_not_called()
