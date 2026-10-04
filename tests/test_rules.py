import json
import re
import unittest
from qbit_seasonal_anime.core.rules import (
    RELEASE_NAME_JOINER,
    build_regex_pattern,
    build_release_name_pattern,
    build_rule_definition,
    build_rule_name,
    sanitize_folder_name,
)
from qbit_seasonal_anime.db.models import Monitored, MonitoredStatus

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


class TestRules(unittest.TestCase):
    def test_sanitize_folder_name(self):
        self.assertEqual(sanitize_folder_name("Ranma 1/2 (2024) 3rd Season"), "Ranma 1-2 (2024) 3rd Season")
        self.assertEqual(sanitize_folder_name("Fate/stay night [Heaven's Feel]"), "Fate-stay night [Heaven's Feel]")
        self.assertEqual(sanitize_folder_name("BLEACH: Sennen Kessen-hen - Kashin-tan"), "BLEACH - Sennen Kessen-hen - Kashin-tan")
        self.assertEqual(sanitize_folder_name('Anime With: "Quotes" & <Brackets>? *'), "Anime With - Quotes & Brackets")
        self.assertEqual(sanitize_folder_name("Re:Zero kara Hajimeru Isekai Seikatsu"), "Re - Zero kara Hajimeru Isekai Seikatsu")
        self.assertEqual(sanitize_folder_name(""), "Anime")

    def test_build_regex_pattern_broad(self):
        aliases = ["Sousou no Frieren", "Frieren: Beyond Journey's End"]
        pattern = build_regex_pattern(aliases)
        self.assertTrue(pattern.startswith("("))
        self.assertTrue(re.search(pattern, "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", re.IGNORECASE))
        self.assertTrue(re.search(pattern, "[Erai-raws] Frieren: Beyond Journey's End - 08.mkv", re.IGNORECASE))
        self.assertFalse(re.search(pattern, "[SubsPlease] Dandadan - 01.mkv", re.IGNORECASE))

    def test_build_regex_pattern_simple_matched_title(self):
        pattern = build_regex_pattern(
            aliases=["Mushoku Tensei: Isekai Ittara Honki Dasu 3rd Season", "Mushoku Tensei S3"],
            matched_title="Mushoku Tensei S3",
        )
        self.assertIn(f"Mushoku{RELEASE_NAME_JOINER}Tensei{RELEASE_NAME_JOINER}S3", pattern)
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
        # Separator tolerance is still needed: groups are inconsistent about joining words.
        self.assertTrue(re.search(pattern, "[Erai-raws] Koori.no.Jouheki.2nd.Season - 03 [1080p].mkv", re.IGNORECASE))
        # The un-numbered season 1 release must not be pulled in by the hedged "2" variant.
        self.assertFalse(re.search(pattern, "[Erai-raws] Koori no Jouheki - 01 [1080p].mkv", re.IGNORECASE))

    def test_build_release_name_pattern_tolerates_separators(self):
        pattern = build_release_name_pattern("Sousou no Frieren")
        self.assertEqual(pattern, f"Sousou{RELEASE_NAME_JOINER}no{RELEASE_NAME_JOINER}Frieren")
        self.assertTrue(re.search(pattern, "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", re.IGNORECASE))
        self.assertTrue(re.search(pattern, "[Erai-raws] Sousou.no.Frieren - 09.mkv", re.IGNORECASE))
        self.assertTrue(re.search(pattern, "[Erai-raws] Sousou_no_Frieren - 10.mkv", re.IGNORECASE))
        self.assertTrue(re.search(pattern, "[Erai-raws] Sousou-No-Frieren - 11.mkv", re.IGNORECASE))
        self.assertFalse(re.search(pattern, "[SubsPlease] Sousou no Kappa - 01 (1080p).mkv", re.IGNORECASE))

    def test_build_release_name_pattern_requires_a_separator_between_tokens(self):
        pattern = build_release_name_pattern("Sousou no Frieren")
        self.assertIn(RELEASE_NAME_JOINER, pattern)
        # Zero-width joins would let words run together, so a separator is required.
        self.assertFalse(re.search(pattern, "[Erai-raws] SousounoFrieren - 08.mkv", re.IGNORECASE))
        self.assertFalse(re.search(pattern, "[Erai-raws] Sousou_noFrieren - 08.mkv", re.IGNORECASE))

    def test_build_release_name_pattern_escapes_metacharacters(self):
        self.assertEqual(
            build_release_name_pattern("Fate/stay night"),
            f"Fate{RELEASE_NAME_JOINER}stay{RELEASE_NAME_JOINER}night",
        )
        self.assertEqual(
            build_release_name_pattern("Re:Zero kara Hajimeru"),
            f"Re{RELEASE_NAME_JOINER}Zero{RELEASE_NAME_JOINER}kara{RELEASE_NAME_JOINER}Hajimeru",
        )
        self.assertTrue(
            re.search(build_release_name_pattern("86 Eighty-Six"), "[SubsPlease] 86 Eighty-Six - 05 (1080p).mkv", re.IGNORECASE)
        )

    def test_build_release_name_pattern_guards_short_names(self):
        self.assertEqual(build_release_name_pattern("SBR"), "SBR")
        self.assertEqual(build_release_name_pattern(""), "")
        self.assertEqual(build_release_name_pattern("   "), "")
        # Too generic to generalize: stays a literal, so it keeps matching its own format.
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
        self.assertIn(f"JoJo{RELEASE_NAME_JOINER}no", learned_pattern)
        for episode in ("01", "02", "11", "24"):
            release = f"[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - {episode} [1080p].mkv"
            self.assertTrue(re.search(learned_pattern, release, re.IGNORECASE), episode)
        self.assertTrue(
            re.search(learned_pattern, "JoJo.no.Kimyou.na.Bouken.Steel.Ball.Run - 12 [1080p].mkv", re.IGNORECASE)
        )
        self.assertFalse(
            re.search(learned_pattern, "[Erai-raws] Some Completely Different Show - 02 [1080p].mkv", re.IGNORECASE)
        )
        self.assertFalse(
            re.search(learned_pattern, "[SubsPlease] Sousou no Frieren - 08 (1080p).mkv", re.IGNORECASE)
        )

    def test_build_rule_definition_uses_learned_tolerant_pattern(self):
        show = Monitored(
            id=7,
            anilist_id=174051,
            display_name="STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
            aliases_json=json.dumps(STEEL_BALL_RUN_ALIASES),
            matched_title="JoJo no Kimyou na Bouken: Steel Ball Run",
            status=MonitoredStatus.FIXED,
        )
        rule_def = build_rule_definition(
            monitored=show,
            feed_url="https://www.erai-raws.info/rss-1080p/",
            base_dir="~/Anime",
        )
        self.assertIn("Steel", rule_def["mustContain"])
        self.assertTrue(re.search(rule_def["mustContain"], ERAI_SBR_RELEASE, re.IGNORECASE))
        self.assertFalse(
            re.search(rule_def["mustContain"], "[Erai-raws] Some Completely Different Show - 02 [1080p].mkv", re.IGNORECASE)
        )

    def test_build_rule_definition(self):
        show = Monitored(
            id=1,
            anilist_id=154587,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren", "Frieren"]',
            save_folder="Sousou no Frieren",
            status=MonitoredStatus.FIXED,
        )
        rule_def = build_rule_definition(
            monitored=show,
            feed_url="https://subsplease.org/rss/?r=1080",
            base_dir="~/Anime",
            category="",
            ratio_limit=1.0,
        )
        self.assertTrue(rule_def["enabled"])
        self.assertTrue(rule_def["useRegex"])
        self.assertFalse(rule_def["smartFilter"])
        self.assertIn("720p", rule_def["mustNotContain"])
        self.assertIn("batch", rule_def["mustNotContain"])
        self.assertEqual(rule_def["affectedFeeds"], ["https://subsplease.org/rss/?r=1080"])
        self.assertEqual(rule_def["assignedCategory"], "")
        self.assertEqual(rule_def["ratioLimit"], 1.0)
        self.assertEqual(rule_def["torrentParams"]["ratio_limit"], 1.0)
        self.assertEqual(rule_def["torrentParams"]["category"], "")
        self.assertTrue(rule_def["savePath"].endswith("Sousou no Frieren"))

        upcoming_show = Monitored(
            id=2,
            anilist_id=999,
            display_name="Upcoming Anime",
            aliases_json='["Upcoming Anime"]',
            status=MonitoredStatus.UNCONFIRMED,
        )
        upcoming_def = build_rule_definition(
            monitored=upcoming_show,
            feed_url="https://subsplease.org/rss/?r=1080",
            base_dir="~/Anime",
        )
        self.assertFalse(upcoming_def["enabled"])

    def test_build_rule_definition_preserves_qbittorrent_owned_state(self):
        show = Monitored(
            id=1,
            anilist_id=154587,
            display_name="Sousou no Frieren",
            aliases_json='["Sousou no Frieren"]',
            status=MonitoredStatus.FIXED,
        )
        previous = {
            "lastMatch": "Fri, 25 Sep 2026 10:53:42 +0000",
            "previouslyMatchedEpisodes": ["08"],
        }

        preserved = build_rule_definition(
            monitored=show,
            feed_url="https://subsplease.org/rss/?r=1080",
            base_dir="~/Anime",
            previous_rule=previous,
        )
        self.assertEqual(preserved["lastMatch"], "Fri, 25 Sep 2026 10:53:42 +0000")
        self.assertEqual(preserved["previouslyMatchedEpisodes"], ["08"])

        # Without a previous rule (brand new rule) the fields stay empty.
        fresh = build_rule_definition(
            monitored=show,
            feed_url="https://subsplease.org/rss/?r=1080",
            base_dir="~/Anime",
        )
        self.assertEqual(fresh["lastMatch"], "")
        self.assertEqual(fresh["previouslyMatchedEpisodes"], [])

    def test_create_or_update_rule_does_not_blank_existing_rule_state(self):
        from unittest.mock import MagicMock
        from qbit_seasonal_anime.db.models import Feed
        from qbit_seasonal_anime.core.rules import create_or_update_rule

        mock_qbit = MagicMock()
        show = Monitored(
            id=1,
            anilist_id=1,
            display_name="Frieren",
            aliases_json='["Sousou no Frieren"]',
            qbit_rule_name="[Seasonal] Frieren",
            status=MonitoredStatus.FIXED,
        )
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss")
        mock_qbit.get_rss_rules.return_value = {
            "[Seasonal] Frieren": {
                "lastMatch": "Fri, 25 Sep 2026 10:53:42 +0000",
                "previouslyMatchedEpisodes": ["08"],
            }
        }

        create_or_update_rule(
            qbit_client=mock_qbit,
            monitored=show,
            feed=feed,
            base_dir="/tmp/Anime",
        )

        written = mock_qbit.set_rss_rule.call_args.kwargs["rule_def"]
        self.assertEqual(written["lastMatch"], "Fri, 25 Sep 2026 10:53:42 +0000")
        self.assertEqual(written["previouslyMatchedEpisodes"], ["08"])

    def test_create_or_update_rule_survives_rule_read_failure(self):
        from unittest.mock import MagicMock
        from qbit_seasonal_anime.db.models import Feed
        from qbit_seasonal_anime.clients.qbit import QbitClientError
        from qbit_seasonal_anime.core.rules import create_or_update_rule

        mock_qbit = MagicMock()
        mock_qbit.get_rss_rules.side_effect = QbitClientError("qBittorrent busy")
        show = Monitored(
            id=1,
            anilist_id=1,
            display_name="Frieren",
            aliases_json='["Sousou no Frieren"]',
            qbit_rule_name="[Seasonal] Frieren",
            status=MonitoredStatus.FIXED,
        )
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss")

        create_or_update_rule(qbit_client=mock_qbit, monitored=show, feed=feed, base_dir="/tmp/Anime")

        written = mock_qbit.set_rss_rule.call_args.kwargs["rule_def"]
        self.assertEqual(written["lastMatch"], "")
        self.assertEqual(written["previouslyMatchedEpisodes"], [])

    def test_match_state_alone_does_not_trigger_a_rule_rewrite(self):
        from qbit_seasonal_anime.core.supervisor import _rules_are_equivalent

        base = {
            "mustContain": "Sousou",
            "mustNotContain": "720p",
            "affectedFeeds": ["https://subsplease.org/rss"],
            "savePath": "/tmp/Anime/Sousou no Frieren",
            "assignedCategory": "Seasonal",
            "useRegex": True,
            "enabled": True,
        }
        current = dict(base, lastMatch="Fri, 25 Sep 2026 10:53:42 +0000", previouslyMatchedEpisodes=["08"])
        desired = dict(base, lastMatch="", previouslyMatchedEpisodes=[])
        self.assertTrue(_rules_are_equivalent(current, desired))

        drifted = dict(base, mustContain="Something Else")
        self.assertFalse(_rules_are_equivalent(current, drifted))

    def test_build_rule_name(self):
        name = build_rule_name(42, "Frieren: Beyond Journey's End")
        self.assertEqual(name, "[Seasonal] Frieren - Beyond Journey's End")

    def test_generate_season_variants_does_not_corrupt_pronoun_i(self):
        from qbit_seasonal_anime.core.rules import generate_season_variants
        variants = generate_season_variants("I Was Reincarnated as a Slime")
        self.assertEqual(variants, ["I Was Reincarnated as a Slime"])

        v2 = generate_season_variants("Mushoku Tensei III")
        self.assertIn("Mushoku Tensei S0?3", v2)

    def test_generate_season_variants_preserves_colon_titles(self):
        """Separators inside a title are part of the name, not a cut point."""
        from qbit_seasonal_anime.core.rules import generate_season_variants
        v = generate_season_variants("Re:Zero kara Hajimeru Isekai Seikatsu 4th Season")
        self.assertIn("Re:Zero kara Hajimeru Isekai Seikatsu S0?4", v)
        self.assertIn("Re:Zero kara Hajimeru Isekai Seikatsu Season 4", v)

        v2 = generate_season_variants("Kaguya-sama: Love Is War Season 2")
        self.assertIn("Kaguya-sama: Love Is War S0?2", v2)

        v3 = generate_season_variants("Fate/stay night: Unlimited Blade Works 2nd Season")
        self.assertIn("Fate/stay night: Unlimited Blade Works S0?2", v3)

    def test_generate_season_variants_keeps_hyphenated_titles_whole(self):
        from qbit_seasonal_anime.core.rules import generate_season_variants
        v = generate_season_variants("Akane-banashi 2nd Season")
        self.assertIn("Akane-banashi S0?2", v)
        self.assertIn("Akane-banashi Season 2", v)

    def test_generate_season_variants_strips_dangling_punctuation(self):
        from qbit_seasonal_anime.core.rules import generate_season_variants
        v = generate_season_variants("Show Name - Season 2")
        self.assertIn("Show Name S0?2", v)
        self.assertNotIn("Show Name - S0?2", v)

    def test_season_variant_regex_matches_unpadded_and_padded_season(self):
        from qbit_seasonal_anime.core.rules import build_regex_pattern
        pattern = build_regex_pattern(["Blue Box Season 2"])
        self.assertTrue(re.search(pattern, "[VARYG] Blue Box S02E01 Deja Vu 1080p NF WEB-DL", re.IGNORECASE))
        self.assertTrue(re.search(pattern, "[SubsPlease] Blue Box S2 - 01 (1080p)", re.IGNORECASE))
        self.assertFalse(re.search(pattern, "[VARYG] Blue Box S03E01 1080p", re.IGNORECASE))

    def test_monitored_aliases_property_safe_json(self):
        show = Monitored(anilist_id=999, display_name="Test", aliases_json="invalid json")
        self.assertEqual(show.aliases, [])

        show2 = Monitored(anilist_id=998, display_name="Test 2", aliases_json='"not a list"')
        self.assertEqual(show2.aliases, [])

    def test_create_or_update_rule_ensures_category(self):
        from unittest.mock import MagicMock
        from qbit_seasonal_anime.core.rules import create_or_update_rule
        from qbit_seasonal_anime.db.models import Feed

        mock_qbit = MagicMock()
        mock_qbit.get_matching_articles.return_value = {"feed_url": ["[SubsPlease] Frieren - 08.mkv"]}
        show = Monitored(id=1, anilist_id=101, display_name="Frieren", aliases_json='["Frieren"]')
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss")

        rule_name = create_or_update_rule(
            qbit_client=mock_qbit,
            monitored=show,
            feed=feed,
            base_dir="~/Anime",
            category="Anime",
            ratio_limit=1.0,
        )
        self.assertEqual(rule_name, "[Seasonal] Frieren")
        mock_qbit.ensure_category_exists.assert_called_with("Anime")
        mock_qbit.set_rss_rule.assert_called_once()
        mock_qbit.get_matching_articles.assert_not_called()

    def test_create_or_update_rule_runs_debug_check_when_enabled(self):
        from unittest.mock import MagicMock, patch
        from qbit_seasonal_anime.core.rules import create_or_update_rule
        from qbit_seasonal_anime.db.models import Feed

        mock_qbit = MagicMock()
        mock_qbit.get_matching_articles.return_value = {"feed_url": ["[SubsPlease] Frieren - 08.mkv"]}
        show = Monitored(id=1, anilist_id=101, display_name="Frieren", aliases_json='["Frieren"]')
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss")

        with patch("qbit_seasonal_anime.core.rules.logger.isEnabledFor", return_value=True):
            create_or_update_rule(
                qbit_client=mock_qbit,
                monitored=show,
                feed=feed,
                base_dir="~/Anime",
                category="Anime",
                ratio_limit=1.0,
            )

        mock_qbit.get_matching_articles.assert_called_once_with("[Seasonal] Frieren")

    def test_create_or_update_rule_reuses_cycle_category_cache(self):
        from unittest.mock import MagicMock
        from qbit_seasonal_anime.core.rules import create_or_update_rule
        from qbit_seasonal_anime.db.models import Feed

        mock_qbit = MagicMock()
        show = Monitored(id=1, anilist_id=101, display_name="Frieren", aliases_json='["Frieren"]')
        feed = Feed(id=1, qbit_feed_name="SubsPlease", qbit_feed_url="https://subsplease.org/rss")
        known_categories = set()

        for _ in range(2):
            create_or_update_rule(
                qbit_client=mock_qbit,
                monitored=show,
                feed=feed,
                base_dir="~/Anime",
                category="Anime",
                ratio_limit=1.0,
                known_categories=known_categories,
            )

        mock_qbit.ensure_category_exists.assert_called_once_with("Anime")
        self.assertEqual(known_categories, {"Anime"})

    def test_resolve_save_path_placeholders(self):
        from qbit_seasonal_anime.core.rules import resolve_save_path
        import os
        home = os.path.expanduser("~")

        p1 = resolve_save_path("~/Anime/{name}", "BLEACH: Sennen Kessen-hen")
        self.assertEqual(p1, f"{home}/Anime/BLEACH - Sennen Kessen-hen")

        p2 = resolve_save_path("~/Anime", "Sousou no Frieren")
        self.assertEqual(p2, f"{home}/Anime/Sousou no Frieren")

        p3 = resolve_save_path("~/Anime/{name}", "Dandadan", custom_save_folder="~/Downloads/{name}")
        self.assertEqual(p3, f"{home}/Downloads/Dandadan")

        p4 = resolve_save_path("~/Anime/{name}", "Aoashi", custom_save_folder="Aoashi 2nd Season")
        self.assertEqual(p4, f"{home}/Anime/Aoashi 2nd Season")


if __name__ == "__main__":
    unittest.main()
