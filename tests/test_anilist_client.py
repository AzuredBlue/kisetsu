from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, patch
from qbit_seasonal_anime.clients.anilist import AniListClient, get_current_and_next_season


def test_season_calculation():
    winter_dt = datetime(2026, 2, 15, tzinfo=timezone.utc)
    cur, nxt = get_current_and_next_season(winter_dt)
    assert cur == ("WINTER", 2026)
    assert nxt == ("SPRING", 2026)

    fall_dt = datetime(2026, 11, 10, tzinfo=timezone.utc)
    cur, nxt = get_current_and_next_season(fall_dt)
    assert cur == ("FALL", 2026)
    assert nxt == ("WINTER", 2027)


@pytest.mark.asyncio
async def test_fetch_user_seasonal_anime_filtering():
    client = AniListClient()

    payload = {
        "MediaListCollection": {
            "lists": [
                {
                    "name": "Planning",
                    "entries": [
                        {
                            "media": {
                                "id": 1,
                                "title": {"romaji": "Currently Airing Show"},
                                "status": "RELEASING",
                                "nextAiringEpisode": {"episode": 3, "airingAt": 1700000000},
                            }
                        },
                        {
                            "media": {
                                "id": 2,
                                "title": {"romaji": "Next Season Upcoming Show"},
                                "status": "NOT_YET_RELEASED",
                                "season": "SPRING",
                                "seasonYear": 2026,
                            }
                        },
                        {
                            "media": {
                                "id": 3,
                                "title": {"romaji": "Old Finished Backlog Show"},
                                "status": "FINISHED",
                                "season": "FALL",
                                "seasonYear": 2015,
                                "nextAiringEpisode": None,
                            }
                        },
                        {
                            "media": {
                                "id": 4,
                                "title": {"romaji": "Far Future Planned Show (2+ Seasons Away)"},
                                "status": "NOT_YET_RELEASED",
                                "season": "WINTER",
                                "seasonYear": 2027,
                                "nextAiringEpisode": {"episode": 1, "airingAt": 1800000000},
                            }
                        },
                    ]
                }
            ]
        }
    }

    with patch.object(client, "_post_query", new_callable=AsyncMock) as mock_post, \
         patch("qbit_seasonal_anime.clients.anilist.get_current_and_next_season", return_value=(("WINTER", 2026), ("SPRING", 2026))):
        mock_post.return_value = payload

        shows = await client.fetch_user_seasonal_anime("TestUser")
        assert len(shows) == 2
        assert {s["anilist_id"] for s in shows} == {1, 2}


@pytest.mark.asyncio
async def test_fetch_user_seasonal_anime_includes_current_season_finished_and_monitored():
    client = AniListClient()

    payload = {
        "MediaListCollection": {
            "lists": [
                {
                    "name": "Watching",
                    "entries": [
                        {
                            "media": {
                                "id": 10,
                                "title": {"romaji": "Summer Finished Show"},
                                "status": "FINISHED",
                                "season": "SUMMER",
                                "seasonYear": 2026,
                                "episodes": 12,
                                "nextAiringEpisode": None,
                            }
                        },
                        {
                            "media": {
                                "id": 20,
                                "title": {"romaji": "Extending Cour from Spring now Finished"},
                                "status": "FINISHED",
                                "season": "SPRING",
                                "seasonYear": 2026,
                                "episodes": 24,
                                "nextAiringEpisode": None,
                            }
                        },
                        {
                            "media": {
                                "id": 30,
                                "title": {"romaji": "Unmonitored Old Spring Backlog"},
                                "status": "FINISHED",
                                "season": "SPRING",
                                "seasonYear": 2026,
                                "episodes": 12,
                                "nextAiringEpisode": None,
                            }
                        },
                    ]
                }
            ]
        }
    }

    with patch.object(client, "_post_query", new_callable=AsyncMock) as mock_post, \
         patch("qbit_seasonal_anime.clients.anilist.get_current_and_next_season", return_value=(("SUMMER", 2026), ("FALL", 2026))):
        mock_post.return_value = payload

        shows = await client.fetch_user_seasonal_anime("TestUser", monitored_anilist_ids={20})
        assert len(shows) == 2
        assert {s["anilist_id"] for s in shows} == {10, 20}


@pytest.mark.asyncio
async def test_fetch_user_seasonal_anime_surfaces_list_status():
    """The entry's own list status must be reported, separate from the show's broadcast status."""
    client = AniListClient()

    payload = {
        "MediaListCollection": {
            "lists": [
                {
                    "name": "Watching",
                    "entries": [
                        {
                            "status": "CURRENT",
                            "media": {
                                "id": 5,
                                "title": {"romaji": "Still Watching"},
                                "status": "RELEASING",
                                "season": "SUMMER",
                                "seasonYear": 2026,
                                "episodes": 12,
                                "nextAiringEpisode": {"episode": 3, "airingAt": 1700000000},
                            },
                        },
                        {
                            "status": "COMPLETED",
                            "media": {
                                "id": 6,
                                "title": {"romaji": "Finished Watching"},
                                "status": "RELEASING",
                                "season": "SUMMER",
                                "seasonYear": 2026,
                                "episodes": 12,
                                "nextAiringEpisode": {"episode": 7, "airingAt": 1700000000},
                            },
                        },
                    ],
                }
            ]
        }
    }

    with patch.object(client, "_post_query", new_callable=AsyncMock) as mock_post, \
         patch("qbit_seasonal_anime.clients.anilist.get_current_and_next_season", return_value=(("SUMMER", 2026), ("FALL", 2026))):
        mock_post.return_value = payload

        shows = {s["anilist_id"]: s for s in await client.fetch_user_seasonal_anime("TestUser", monitored_anilist_ids={5, 6})}

    assert shows[5]["list_status"] == "CURRENT"
    assert shows[6]["list_status"] == "COMPLETED"
    # Both are still RELEASING on AniList, so the list status is the only difference.
    assert shows[5]["status"] == shows[6]["status"] == "RELEASING"


@pytest.mark.asyncio
async def test_fetch_user_seasonal_anime_returns_completed_entries_for_monitored_shows():
    """A list-completed show is no longer CURRENT/PLANNING, but must survive for monitored ids."""
    client = AniListClient()

    payload = {
        "MediaListCollection": {
            "lists": [
                {
                    "name": "Completed",
                    "entries": [
                        {
                            "status": "COMPLETED",
                            "media": {
                                "id": 7,
                                "title": {"romaji": "Watched To The End"},
                                "status": "FINISHED",
                                "season": "SPRING",
                                "seasonYear": 2024,
                                "episodes": 12,
                                "nextAiringEpisode": None,
                            },
                        },
                        {
                            "status": "COMPLETED",
                            "media": {
                                "id": 8,
                                "title": {"romaji": "Unwatched Backlog Entry"},
                                "status": "FINISHED",
                                "season": "SPRING",
                                "seasonYear": 2015,
                                "episodes": 12,
                                "nextAiringEpisode": None,
                            },
                        },
                    ],
                }
            ]
        }
    }

    with patch.object(client, "_post_query", new_callable=AsyncMock) as mock_post, \
         patch("qbit_seasonal_anime.clients.anilist.get_current_and_next_season", return_value=(("SUMMER", 2026), ("FALL", 2026))):
        mock_post.return_value = payload

        shows = {s["anilist_id"]: s for s in await client.fetch_user_seasonal_anime("TestUser", monitored_anilist_ids={7})}

    assert set(shows) == {7}
    assert shows[7]["list_status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_fetch_user_seasonal_anime_never_introduces_a_list_completed_show():
    """A finished-watching entry must not be added as a new show, even if its media still looks current."""
    client = AniListClient()

    payload = {
        "MediaListCollection": {
            "lists": [
                {
                    "name": "Completed",
                    "entries": [
                        {
                            "status": "COMPLETED",
                            "media": {
                                "id": 9,
                                "title": {"romaji": "Old Season Shown The User Already Finished"},
                                # matches is_currently_releasing and is_current_season_finished
                                "status": "RELEASING",
                                "season": "SUMMER",
                                "seasonYear": 2026,
                                "episodes": 12,
                                "nextAiringEpisode": {"episode": 4, "airingAt": 1700000000},
                            },
                        }
                    ],
                }
            ]
        }
    }

    with patch.object(client, "_post_query", new_callable=AsyncMock) as mock_post, \
         patch("qbit_seasonal_anime.clients.anilist.get_current_and_next_season", return_value=(("SUMMER", 2026), ("FALL", 2026))):
        mock_post.return_value = payload

        # Not monitored yet, so it must be filtered out instead of armed for download.
        assert await client.fetch_user_seasonal_anime("TestUser", monitored_anilist_ids={1, 2}) == []
        # Already monitored, so it comes through for the caller to complete.
        shows = await client.fetch_user_seasonal_anime("TestUser", monitored_anilist_ids={9})
        assert [s["anilist_id"] for s in shows] == [9]

