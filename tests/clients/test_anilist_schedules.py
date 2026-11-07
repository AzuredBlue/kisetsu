import asyncio
import re
from datetime import datetime, timezone

from kisetsu.clients import anilist
from kisetsu.clients.anilist import AniListClient

BLOCK = re.compile(r"(p\d+): Page\(page: (\d+), perPage: 50\) \{ pageInfo \{ hasNextPage \} airingSchedules\((.*?), sort: TIME\)")
T0 = 1_780_000_000


class FakeAniList:
    """Answers aliased schedule pages the way AniList does, from air times by show; ``broken`` shows answer with an error."""

    def __init__(self, schedules, broken=()):
        self.schedules = schedules
        self.broken = set(broken)
        self.requests = []

    async def post(self, query, variables, allow_partial=False, kind=None):
        self.requests.append(query)
        data = {}
        for alias, page, where in BLOCK.findall(query):
            single = re.fullmatch(r"mediaId: (\d+)", where)
            ids = [int(single.group(1))] if single else [int(i) for i in re.search(r"mediaId_in: \[(.*?)\]", where).group(1).split(", ")]
            after = int(re.search(r"airingAt_greater: (\d+)", where).group(1)) if "airingAt_greater" in where else None
            if self.broken & set(ids):
                data[alias] = None
                data["__errors__"] = ["broken"]
                continue
            nodes = sorted(
                ({"mediaId": i, "episode": e, "airingAt": at} for i in ids for e, at in self.schedules.get(i, [])
                 if after is None or at > after),
                key=lambda n: n["airingAt"],
            )
            start = (int(page) - 1) * 50
            data[alias] = {"pageInfo": {"hasNextPage": len(nodes) > start + 50}, "airingSchedules": nodes[start:start + 50]}
        return data


def weekly(episodes, first=T0):
    return [(n, first + (n - 1) * 7 * 86400) for n in range(1, episodes + 1)]


def client_with(fake):
    client = AniListClient()
    client._post_query = fake.post
    return client


def test_many_shows_air_times_come_in_one_request_of_several_pages():
    # 13 shows of 24 episodes: 312 air times, seven pages, all asked for at once.
    fake = FakeAniList({media_id: weekly(24) for media_id in range(1, 14)} | {99: []})
    got = asyncio.run(client_with(fake).fetch_upcoming_airing_schedules(list(range(1, 14)) + [99], datetime.fromtimestamp(T0 - 1, timezone.utc)))
    assert len(fake.requests) == 1
    assert [e["episode"] for e in got[5]] == list(range(1, 25)) and got[99] == []
    assert got[5][0]["airing_at"] == datetime.fromtimestamp(T0, timezone.utc)


def test_pages_beyond_one_request_follow_in_the_next():
    fake = FakeAniList({1: weekly(500)})
    got = asyncio.run(client_with(fake).fetch_upcoming_airing_schedules([1], datetime.fromtimestamp(T0 - 1, timezone.utc)))
    assert len(fake.requests) == 2 and len(got[1]) == 500


def test_full_schedules_of_several_shows_share_requests():
    fake = FakeAniList({1: weekly(60), 2: weekly(3), 3: weekly(12)})
    got = asyncio.run(client_with(fake).fetch_media_airing_schedules([1, 2, 3]))
    assert len(fake.requests) == 1
    assert {media_id: len(schedule) for media_id, schedule in got.items()} == {1: 60, 2: 3, 3: 12}


def test_a_show_answered_with_an_error_costs_only_its_own_schedule():
    fake = FakeAniList({1: weekly(3), 2: weekly(3)}, broken={2})
    assert set(asyncio.run(client_with(fake).fetch_media_airing_schedules([1, 2]))) == {1}
    # A batch with a broken show in it is fetched show by show: the others still come.
    got = asyncio.run(client_with(fake).fetch_upcoming_airing_schedules([1, 2], datetime.fromtimestamp(T0 - 1, timezone.utc)))
    assert set(got) == {1}


def test_one_request_never_asks_for_more_pages_than_its_share(monkeypatch):
    monkeypatch.setattr(anilist, "PAGES_PER_REQUEST", 4)
    fake = FakeAniList({media_id: weekly(3) for media_id in range(1, 11)})
    got = asyncio.run(client_with(fake).fetch_media_airing_schedules(list(range(1, 11))))
    assert len(got) == 10 and len(fake.requests) == 3
    assert all(len(BLOCK.findall(query)) <= 4 for query in fake.requests)
