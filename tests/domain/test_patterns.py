import os
import re
import unittest
from pathlib import Path
from kisetsu.domain.patterns import (
    build_regex_pattern,
    build_release_name_pattern,
    generate_season_variants,
    resolve_save_path,
    sanitize_folder_name,
)


STEEL_BALL_RUN_ALIASES = [
    "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd - 3rd STAGE",
    "STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
    "SBR",
    "JoJo's Bizarre Adventure: Part 7\u2013Steel Ball Run",
]
ERAI_SBR_RELEASE = (
    "[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - 02 "
    "[1080p NF WEB-DL AVC AAC][MultiSub][78128421]"
)




class TestPatterns(unittest.TestCase):
    def test_sanitize_folder_name(self):
        self.assertEqual(sanitize_folder_name("Ranma 1/2 (2024) 3rd Season"), "Ranma 1-2 (2024) 3rd Season")
        self.assertEqual(sanitize_folder_name("Fate/stay night [Heaven's Feel]"), "Fate-stay night [Heaven's Feel]")
        self.assertEqual(sanitize_folder_name("BLEACH: Sennen Kessen-hen - Kashin-tan"), "BLEACH - Sennen Kessen-hen - Kashin-tan")
        self.assertEqual(sanitize_folder_name('Anime With: "Quotes" & <Brackets>? *'), "Anime With - Quotes & Brackets")
        self.assertEqual(sanitize_folder_name("Re:Zero kara Hajimeru Isekai Seikatsu"), "Re - Zero kara Hajimeru Isekai Seikatsu")
        self.assertEqual(sanitize_folder_name(""), "Anime")

    def test_a_learned_pattern_from_a_parsed_title_survives_every_separator_style(self):
        """
        The learned pattern is built from the series name alone.

        The direct engine used to learn the whole release filename, which pinned
        one episode's number, quality and group into the rule and matched nothing
        but the episode it came from.
        """
        pattern = build_regex_pattern(aliases=[], matched_title="Blue.Box")
        self.assertEqual(pattern, r"Blue\.Box")
        self.assertTrue(
            re.search(pattern, "[Erai-raws]  Blue.Box.S02E02.Adequate.1080p.CR.WEB-DL.AAC2.0-HYDE.mkv", re.IGNORECASE)
        )
        self.assertFalse(re.search(pattern, "[Group] BlueXBox - 04.mkv", re.IGNORECASE))

    def test_build_regex_pattern_broad(self):
        aliases = ["Sousou no Frieren", "Frieren: Beyond Journey's End"]
        pattern = build_regex_pattern(aliases)
        self.assertTrue(pattern.startswith("("))
        self.assertTrue(re.search(pattern, "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", re.IGNORECASE))
        self.assertTrue(re.search(pattern, "[Erai-raws] Frieren: Beyond Journey's End - 08.mkv", re.IGNORECASE))
        self.assertFalse(re.search(pattern, "[SubsPlease] Dandadan - 01.mkv", re.IGNORECASE))

    def test_numeric_season_variants_do_not_match_a_longer_number(self):
        pattern = build_regex_pattern(["Mushoku Tensei Season 3"])
        self.assertTrue(re.search(pattern, "[X] Mushoku Tensei 3 - 02 (1080p).mkv", re.IGNORECASE))
        self.assertTrue(re.search(pattern, "Mushoku Tensei S03E01 1080p", re.IGNORECASE))
        self.assertFalse(re.search(pattern, "[X] Mushoku Tensei 30 - 02 (1080p).mkv", re.IGNORECASE))

    def test_build_regex_pattern_simple_matched_title(self):
        pattern = build_regex_pattern(
            aliases=["Mushoku Tensei: Isekai Ittara Honki Dasu 3rd Season", "Mushoku Tensei S3"],
            matched_title="Mushoku Tensei S3",
        )
        self.assertIn("Mushoku Tensei S3", pattern)
        self.assertTrue(re.search(pattern, "[SubsPlease] Mushoku Tensei S3 - 09 (1080p) [DDF202A0].mkv", re.IGNORECASE))
        self.assertTrue(re.search(pattern, "[Erai-raws] Mushoku Tensei S3 - 09 (1080p).mkv", re.IGNORECASE))
        self.assertFalse(re.search(pattern, "[SubsPlease] Bleach - 45.mkv", re.IGNORECASE))

    def test_learned_rule_does_not_invent_season_variants(self):
        pattern = build_regex_pattern(
            aliases=["Koori no Jouheki 2nd Season"],
            matched_title="Koori no Jouheki 2nd Season",
        )
        self.assertNotIn("|", pattern)
        self.assertTrue(
            re.search(pattern, "[Erai-raws] Koori no Jouheki 2nd Season - 02 [1080p NF WEB-DL][MultiSub].mkv", re.IGNORECASE)
        )
        # The un-numbered season 1 release must not be pulled in by the hedged "2" variant.
        self.assertFalse(re.search(pattern, "[Erai-raws] Koori no Jouheki - 01 [1080p].mkv", re.IGNORECASE))

    def test_build_release_name_pattern_is_the_observed_name(self):
        pattern = build_release_name_pattern("Sousou no Frieren")
        self.assertEqual(pattern, "Sousou no Frieren")
        self.assertTrue(re.search(pattern, "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", re.IGNORECASE))
        self.assertFalse(re.search(pattern, "[Erai-raws] Sousou.no.Frieren - 09.mkv", re.IGNORECASE))
        self.assertFalse(re.search(pattern, "[SubsPlease] Sousou no Kappa - 01 (1080p).mkv", re.IGNORECASE))

    def test_build_release_name_pattern_collapses_whitespace(self):
        self.assertEqual(build_release_name_pattern("  Sousou   no Frieren "), "Sousou no Frieren")

    def test_build_release_name_pattern_escapes_metacharacters(self):
        self.assertEqual(build_release_name_pattern("Fate/stay night"), "Fate/stay night")
        self.assertEqual(build_release_name_pattern("Re:Zero kara Hajimeru"), "Re:Zero kara Hajimeru")
        self.assertEqual(build_release_name_pattern("Kaguya-sama (Part 2)"), r"Kaguya-sama \(Part 2\)")
        self.assertTrue(
            re.search(build_release_name_pattern("86 Eighty-Six"), "[SubsPlease] 86 Eighty-Six - 05 (1080p).mkv", re.IGNORECASE)
        )

    def test_build_release_name_pattern_guards_short_names(self):
        self.assertEqual(build_release_name_pattern("SBR"), "SBR")
        self.assertEqual(build_release_name_pattern(""), "")
        self.assertEqual(build_release_name_pattern("   "), "")
        self.assertTrue(re.search(build_release_name_pattern("SBR"), "[Erai-raws] SBR - 01 [1080p].mkv", re.IGNORECASE))

    def test_build_release_name_pattern_bounds_bare_numeric_tokens(self):
        pattern = build_regex_pattern(["Sousou no Frieren 2"], matched_title="Sousou no Frieren 2")
        self.assertTrue(re.search(pattern, "[SubsPlease] Sousou no Frieren 2 - 04 (1080p).mkv", re.IGNORECASE))
        self.assertFalse(re.search(pattern, "[SubsPlease] Sousou no Frieren 20 - 04 (1080p).mkv", re.IGNORECASE))

    def test_build_regex_pattern_from_steel_ball_run_release(self):
        armed_pattern = build_regex_pattern(STEEL_BALL_RUN_ALIASES)
        self.assertFalse(re.search(armed_pattern, ERAI_SBR_RELEASE, re.IGNORECASE))

        learned_pattern = build_regex_pattern(
            STEEL_BALL_RUN_ALIASES,
            matched_title="JoJo no Kimyou na Bouken: Steel Ball Run",
        )
        self.assertIn("JoJo no Kimyou", learned_pattern)
        for episode in ("01", "02", "11", "24"):
            release = f"[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - {episode} [1080p].mkv"
            self.assertTrue(re.search(learned_pattern, release, re.IGNORECASE), episode)
        self.assertFalse(
            re.search(learned_pattern, "[Erai-raws] Some Completely Different Show - 02 [1080p].mkv", re.IGNORECASE)
        )
        self.assertFalse(
            re.search(learned_pattern, "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", re.IGNORECASE)
        )

    def test_generate_season_variants_does_not_corrupt_pronoun_i(self):

        variants = generate_season_variants("I Was Reincarnated as a Slime")
        self.assertEqual(variants, ["I Was Reincarnated as a Slime"])

        v2 = generate_season_variants("Mushoku Tensei III")
        self.assertIn("Mushoku Tensei S0?3", v2)

    def test_generate_season_variants_preserves_colon_titles(self):
        """Separators inside a title are part of the name, not a cut point."""

        v = generate_season_variants("Re:Zero kara Hajimeru Isekai Seikatsu 4th Season")
        self.assertIn("Re:Zero kara Hajimeru Isekai Seikatsu S0?4", v)
        self.assertIn("Re:Zero kara Hajimeru Isekai Seikatsu Season 4", v)

        v2 = generate_season_variants("Kaguya-sama: Love Is War Season 2")
        self.assertIn("Kaguya-sama: Love Is War S0?2", v2)

        v3 = generate_season_variants("Fate/stay night: Unlimited Blade Works 2nd Season")
        self.assertIn("Fate/stay night: Unlimited Blade Works S0?2", v3)

    def test_generate_season_variants_keeps_hyphenated_titles_whole(self):

        v = generate_season_variants("Akane-banashi 2nd Season")
        self.assertIn("Akane-banashi S0?2", v)
        self.assertIn("Akane-banashi Season 2", v)

    def test_generate_season_variants_strips_dangling_punctuation(self):

        v = generate_season_variants("Show Name - Season 2")
        self.assertIn("Show Name S0?2", v)
        self.assertNotIn("Show Name - S0?2", v)

    def test_season_variant_regex_matches_unpadded_and_padded_season(self):

        pattern = build_regex_pattern(["Blue Box Season 2"])
        self.assertTrue(re.search(pattern, "[VARYG] Blue Box S02E01 Deja Vu 1080p NF WEB-DL", re.IGNORECASE))
        self.assertTrue(re.search(pattern, "[SubsPlease] Blue Box S2 - 01 (1080p)", re.IGNORECASE))
        self.assertFalse(re.search(pattern, "[VARYG] Blue Box S03E01 1080p", re.IGNORECASE))

    def test_resolve_save_path_placeholders(self):


        home = os.path.expanduser("~")

        p1 = resolve_save_path("~/Anime/{name}", "BLEACH: Sennen Kessen-hen")
        self.assertEqual(p1, str(Path(home) / "Anime" / "BLEACH - Sennen Kessen-hen"))

        p2 = resolve_save_path("~/Anime", "Sousou no Frieren")
        self.assertEqual(p2, str(Path(home) / "Anime" / "Sousou no Frieren"))

        p3 = resolve_save_path("~/Anime/{name}", "Dandadan", custom_save_folder="~/Downloads/{name}")
        self.assertEqual(p3, str(Path(home) / "Downloads" / "Dandadan"))

        p4 = resolve_save_path("~/Anime/{name}", "Aoashi", custom_save_folder="Aoashi 2nd Season")
        self.assertEqual(p4, str(Path(home) / "Anime" / "Aoashi 2nd Season"))


class TestPremiereOutOfReach(unittest.TestCase):
    def show(self, **fields):
        values = {"anilist_id": 1, "display_name": "Akane-banashi 2nd Season", "anilist_status": "NOT_YET_RELEASED"}
        values.update(fields)
