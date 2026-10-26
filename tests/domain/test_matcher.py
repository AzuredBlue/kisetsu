from kisetsu.domain.matcher import (
    broad_match,
    build_matcher,
    decide,
    decide_dated,
    matching_show_ids,
    narrow_to_feed,
    newest_raw_episode,
    signature,
)

SBR = [
    "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd - 3rd STAGE",
    "STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
    "SBR",
    "JoJo's Bizarre Adventure: Part 7–Steel Ball Run",
]
BLUE_BOX = ["Blue Box Season 2", "Ao no Hako Season 2"]


def accepts(aliases, title, learned=None):
    return decide(build_matcher(aliases, learned), title)[0]


# Arc enforcement

def test_a_split_cour_show_rejects_a_bare_title_of_the_previous_cour():
    assert not accepts(SBR, "[SubsPlease] JoJo no Kimyou na Bouken: Steel Ball Run - 01 (1080p) [A1B2C3].mkv")
    assert not accepts(SBR, "[Erai-raws] SBR - 01v2 [1080p].mkv")


def test_a_release_that_names_the_cour_matches_and_a_different_cour_does_not():
    assert accepts(SBR, "[SubsPlease] STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE - 02 [1080p].mkv")
    assert accepts(SBR, "[SubsPlease] JoJo no Kimyou na Bouken: Steel Ball Run 3rd STAGE - 02 [1080p].mkv")
    assert not accepts(SBR, "[SubsPlease] JoJo no Kimyou na Bouken: Steel Ball Run 1st STAGE - 02 [1080p].mkv")


def test_a_plain_season_marker_is_not_enforced():
    assert accepts(["Sousou no Frieren", "Sousou no Frieren 2nd Season"], "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv")


def test_the_broad_level_keeps_what_the_decision_level_will_refuse():
    matcher = build_matcher(SBR, None)
    title = "[SubsPlease] JoJo no Kimyou na Bouken: Steel Ball Run - 01 (1080p) [A1B2C3].mkv"
    assert broad_match(matcher, title)
    assert not decide(matcher, title)[0]
    assert not broad_match(matcher, "[SubsPlease] Some Other Show - 01 (1080p).mkv")


def test_a_dated_release_on_the_shows_schedule_needs_no_arc_marker():
    matcher = build_matcher(SBR, None)
    title = "[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - 04 [1080p]"
    assert decide_dated(matcher, title, None, lambda raw: raw == 4)[0]


def test_a_release_off_the_schedule_or_naming_another_stage_still_needs_its_marker():
    matcher = build_matcher(SBR, None)
    bare = "[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - 01 [1080p]"
    assert not decide_dated(matcher, bare, None, lambda raw: False)[0]
    other_stage = "[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run 1st Stage - 04 [1080p]"
    assert not decide_dated(matcher, other_stage, None, lambda raw: True)[0]


# A locked feed

HELD = ["[Varyg] Blue.Box.S02E01.Deja.Vu.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-VARYG.mkv"]


def locked(held, learned="Blue.Box"):
    return narrow_to_feed(build_matcher(BLUE_BOX, learned), learned, held)


def test_a_locked_feed_takes_only_the_learned_name_and_season():
    matcher = locked(HELD)
    assert matcher.strict and matcher.season_known and matcher.season == 2
    assert decide(matcher, "[Varyg] Blue.Box.S02E02.Deja.Vu.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-VARYG.mkv")[0]
    assert not decide(matcher, "[Varyg] Blue.Box.S01E02.1080p.NF.WEB-DL.DUAL.DDP5.1.H.264-VARYG.mkv")[0]
    assert not decide(matcher, "[SubsPlease] Ao no Hako Season 2 - 02 (1080p).mkv")[0]


def test_an_unlocked_feed_still_takes_any_alias():
    assert accepts(BLUE_BOX, "[SubsPlease] Ao no Hako Season 2 - 02 (1080p).mkv", learned="Blue.Box")


def test_a_locked_feed_with_no_downloads_yet_does_not_know_its_season():
    matcher = narrow_to_feed(build_matcher(BLUE_BOX, "Blue Box"), "Blue Box", [])
    assert matcher.strict and not matcher.season_known


def test_a_locked_feed_whose_releases_name_no_season_refuses_one_that_does():
    matcher = narrow_to_feed(build_matcher(["Sousou no Frieren"], "Sousou no Frieren"), "Sousou no Frieren", ["[SubsPlease] Sousou no Frieren - 08 (1080p).mkv"])
    assert matcher.season_known and matcher.season is None
    assert decide(matcher, "[SubsPlease] Sousou no Frieren - 09 (1080p).mkv")[0]
    assert not decide(matcher, "[SubsPlease] Sousou no Frieren S2 - 09 (1080p).mkv")[0]


def test_conflicting_seasons_among_downloads_leave_the_season_unchecked():
    matcher = narrow_to_feed(
        build_matcher(BLUE_BOX, "Blue.Box"), "Blue.Box",
        ["[Varyg] Blue.Box.S01E01.mkv", "[Varyg] Blue.Box.S02E01.mkv"],
    )
    assert matcher.strict and not matcher.season_known


def test_a_show_with_no_learned_name_is_not_narrowed():
    matcher = build_matcher(BLUE_BOX, None)
    assert narrow_to_feed(matcher, None, HELD) is matcher


# Helpers

def test_the_newest_raw_episode_is_the_highest_matching_number():
    matcher = build_matcher(["Sousou no Frieren"], None)
    titles = [
        "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv",
        "[SubsPlease] Sousou no Frieren - 10 (1080p).mkv",
        "[SubsPlease] Another Show - 30 (1080p).mkv",
        "",
    ]
    assert newest_raw_episode(matcher, titles) == 10
    assert newest_raw_episode(matcher, []) is None


def test_one_title_can_match_two_followed_shows_and_only_filters_them():
    medalist = build_matcher(["Medalist Movie", "Medalist"], None)
    other = build_matcher(["Medalist Movie", "Rascal Does Not Dream of a Dear Friend"], None)
    matchers = [(1, medalist), (2, other)]
    title = "[Group] Medalist Movie [1080p].mkv"
    assert matching_show_ids(matchers, title) == [1, 2]
    assert matching_show_ids(matchers, title, only=[2]) == [2]
    assert matching_show_ids(matchers, "[Group] Unrelated Show - 01 [1080p].mkv") == []


def test_the_signature_changes_with_aliases_and_learned_names_only():
    base = signature([(1, ["A"], ""), (2, ["B"], "")])
    assert signature([(2, ["B"], ""), (1, ["A"], "")]) == base
    assert signature([(1, ["A", "A2"], ""), (2, ["B"], "")]) != base
    assert signature([(1, ["A"], "Learned"), (2, ["B"], "")]) != base
    assert signature([(1, ["A"], "")]) != base
