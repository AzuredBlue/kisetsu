"""Release names that used to be misread: brackets and symbols in AniList titles, other seasons, recaps,
episode titles after the number, short aliases and spin-offs."""

import pytest

from kisetsu.domain.matcher import build_matcher, decide, matching_show_ids
from kisetsu.domain.parsing import normalize_alias, parse_release_title

# AniList's own titles and synonyms (checked against AniList).
OSHI_3 = ["[Oshi no Ko] 3rd Season", "OSHI NO KO Season 3", "[OSHI NO KO] Season 3", "【OSHI NO KO】Season 3"]
OSHI_FINAL = ["[Oshi no Ko] Final Season", "OSHI NO KO Season 4", "[OSHI NO KO] Season 4", "【OSHI NO KO】Season 4"]
YURU_3 = ["Yuru Camp△ SEASON 3", "Laid-Back Camp Season 3", "搖曳露營△ (第三季)"]


def matches(aliases, title):
    return decide(build_matcher(aliases, None), title, {})[0]


def test_a_title_in_brackets_keeps_its_words():
    assert normalize_alias("[Oshi no Ko] 3rd Season") == "oshi no ko season 3"


@pytest.mark.parametrize("aliases, title", [
    (OSHI_3, "[Erai-raws] Kimi ni Todoke 3rd Season - 05 [1080p][Multiple Subtitle]"),
    (OSHI_3, "[SubsPlease] Kingdom 6th Season - 05 (1080p).mkv"),
    (OSHI_FINAL, "[SubsPlease] Boku no Hero Academia Final Season - 05 (1080p).mkv"),
    (["[Oshi no Ko] 3rd Season"], "[Erai-raws] Any Random Show 3rd Season - 05 [1080p]"),
])
def test_a_bracketed_title_does_not_match_other_shows_seasons(aliases, title):
    assert not matches(aliases, title)


@pytest.mark.parametrize("aliases", [OSHI_3, ["[Oshi no Ko] 3rd Season"]])
def test_a_bracketed_title_matches_its_own_release(aliases):
    assert matches(aliases, "[SubsPlease] Oshi no Ko S3 - 05 (1080p).mkv")


@pytest.mark.parametrize("aliases, title", [
    (YURU_3, "[SubsPlease] Yuru Camp S3 - 05 (1080p).mkv"),
    (YURU_3, "[SubsPlease] Yuru Camp Season 3 - 05 (1080p).mkv"),
    (["SPY×FAMILY Season 3"], "[SubsPlease] Spy x Family S3 - 05 (1080p).mkv"),
    (["SPY×FAMILY Season 3"], "[SubsPlease] Spy x Family Season 3 - 05 (1080p).mkv"),
])
def test_symbols_in_a_title_do_not_stop_the_match(aliases, title):
    assert matches(aliases, title)


@pytest.mark.parametrize("aliases, title", [
    (["Kingdom 6th Season", "Kingdom Season 6"], "[SubsPlease] Kingdom 5th Season - 05 (1080p).mkv"),
    (["One Punch Man 3", "One Punch Man Season 3"], "[SubsPlease] One Punch Man S2 - 05 (1080p).mkv"),
])
def test_a_release_naming_another_season_does_not_match(aliases, title):
    assert not matches(aliases, title)


@pytest.mark.parametrize("aliases, title", [
    (["Kaguya-sama wa Kokurasetai: Ultra Romantic"], "[SubsPlease] Kaguya-sama wa Kokurasetai S3 - 05 (1080p).mkv"),
    (["Ao no Hako 2nd Season", "Blue Box Season 2"], "[SubsPlease] Ao no Hako - 30 (1080p).mkv"),
    (["Kingdom 6th Season", "Kingdom Season 6"], "[SubsPlease] Kingdom 6th Season - 05 (1080p).mkv"),
    (["One Punch Man 3", "One Punch Man Season 3"], "[SubsPlease] One Punch Man S3 - 05 (1080p).mkv"),
])
def test_a_season_named_on_one_side_only_still_matches(aliases, title):
    assert matches(aliases, title)


@pytest.mark.parametrize("title", [
    "[SubsPlease] Shangri-La Frontier - 12.5 (1080p) [ABCDEF12].mkv",
    "[SubsPlease] Kusuriya no Hitorigoto - 24.5 (1080p) [ABCDEF12].mkv",
    "[SubsPlease] Tensei shitara Slime Datta Ken - 48.5 (1080p).mkv",
])
def test_a_half_numbered_recap_has_no_episode(title):
    assert parse_release_title(title)["episode"] is None


@pytest.mark.parametrize("title, episode", [
    ("[SubsPlease] Shangri-La Frontier - 12 (1080p) [ABCDEF12].mkv", 12),
    ("[Erai-raws] 2.5 Jigen no Ririsa - 05 [1080p]", 5),
    ("[SubsPlease] Sousou no Frieren - 05v2 (1080p).mkv", 5),
    ("[Anime Time] Frieren - 05 - The Hero's Party [1080p].mkv", 5),
    ("[Tsundere-Raws] Dandadan - 05 VOSTFR (CR) [WEB 1080p x264 AAC].mkv", 5),
    ("[SubsPlease] 86 - Eighty Six - 05 (1080p).mkv", 5),
    ("[SubsPlease] Dr. Stone - Science Future - 05 (1080p) [ABCDEF12].mkv", 5),
    ("[SubsPlease] Bleach - Sennen Kessen-hen - Soukoku-tan - 27 (1080p).mkv", 27),
])
def test_episode_numbers_are_read(title, episode):
    assert parse_release_title(title)["episode"] == episode


def test_an_episode_title_after_the_number_is_not_part_of_the_show_name():
    assert parse_release_title("[Anime Time] Frieren - 05 - The Hero's Party [1080p].mkv")["title"] == "Frieren"
    assert parse_release_title("[SubsPlease] Dr. Stone - Science Future - 05 (1080p).mkv")["title"] == "Dr. Stone - Science Future"


@pytest.mark.parametrize("aliases, title, expected", [
    (["SBR", "Steel Ball Run"], "[Grp] Hisbro - 05 (1080p)", False),
    (["SBR", "Steel Ball Run"], "[Grp] Absbrand Saga - 05 (1080p)", False),
    (["GITS 2026"], "[Grp] Agits 2026 Special - 01 (1080p)", False),
    (["SBR", "Steel Ball Run"], "[Grp] JoJo SBR - 05 (1080p)", True),
])
def test_a_short_alias_matches_only_as_a_whole_word(aliases, title, expected):
    assert matches(aliases, title) is expected


def test_a_parent_release_goes_to_the_parent_not_its_spin_off():
    shows = [
        (1, build_matcher(["Boku no Hero Academia: FINAL SEASON", "My Hero Academia Final Season"], None)),
        (2, build_matcher(["Vigilante: Boku no Hero Academia Illegals", "My Hero Academia: Vigilantes"], None)),
    ]
    assert matching_show_ids(shows, "[SubsPlease] Boku no Hero Academia - 160 (1080p).mkv", {}) == [1]
    assert matching_show_ids(shows, "[SubsPlease] Vigilante - Boku no Hero Academia Illegals - 05 (1080p).mkv", {}) == [2]
