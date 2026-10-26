"""Deciding which feed titles belong to a show.

Two levels share this module so a release can never be stored but never grabbed, or the
reverse. The broad level (``broad_match``) decides what is kept from a feed at all. The
decision level (``decide``) is what grabbing and discovery use: it enforces a cour or stage
marker the show's aliases insist on, and on a feed the show is locked to it narrows to the
name and season that feed has been delivering.
"""

import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from kisetsu.domain.parsing import (
    alias_arc_requirement,
    match_release_to_show,
    prepare_aliases,
    release_arc_qualifier,
    release_season,
)
from kisetsu.domain.patterns import show_match_patterns

ParsedCache = Dict[str, Dict[str, Any]]


@dataclass(frozen=True)
class Matcher:
    aliases: Tuple[str, ...]
    prepared: Tuple[Tuple[str, str], ...]
    pattern: str
    learned_pattern: Optional[str] = None
    # On a locked feed: only the learned name matches, and the season must agree.
    strict: bool = False
    season_known: bool = False
    season: Optional[int] = None


def build_matcher(aliases: Sequence[str], learned_name: Optional[str]) -> Matcher:
    pattern, learned_pattern = show_match_patterns(list(aliases), learned_name)
    return Matcher(tuple(aliases), tuple(prepare_aliases(list(aliases))), pattern, learned_pattern)


def narrow_to_feed(matcher: Matcher, learned_name: Optional[str], held_titles: Iterable[str]) -> Matcher:
    """``matcher`` narrowed to what a show's locked feed has delivered.

    Only the learned name matches, and a release must name the same season as the releases
    already downloaded from the feed (or none, when they name none).
    """
    if not matcher.learned_pattern:
        return matcher
    held = list(held_titles)
    named = {release_season(title) for title in held} | {release_season(learned_name or "")}
    named.discard(None)
    if len(named) > 1:
        return replace(matcher, strict=True)
    if named:
        return replace(matcher, strict=True, season_known=True, season=next(iter(named)))
    # Nothing downloaded names a season, so the feed does not write one for this show.
    return replace(matcher, strict=True, season_known=bool(held))


def broad_match(matcher: Matcher, title: str, parsed_cache: Optional[ParsedCache] = None) -> bool:
    """Whether the title could be one of the show's releases; the arc marker is not enforced."""
    return match_release_to_show(
        title,
        list(matcher.aliases),
        test_pattern=matcher.pattern,
        prepared_aliases=list(matcher.prepared),
        parsed_cache=parsed_cache,
        ignore_arc_marker=True,
    )[0]


def decide(
    matcher: Matcher,
    title: str,
    parsed_cache: Optional[ParsedCache] = None,
    unmarked_ok: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    # A title in the learned name that carries no arc marker shows that the group does not
    # label this show's cour, so there is nothing to compare with the aliases' marker. A
    # title with a marker of its own is still checked.
    learned_without_marker = bool(
        matcher.learned_pattern
        and release_arc_qualifier(title) is None
        and re.search(matcher.learned_pattern, title, re.IGNORECASE)
    )
    matched, _, parsed = match_release_to_show(
        title,
        list(matcher.aliases),
        test_pattern=matcher.learned_pattern if matcher.strict else matcher.pattern,
        prepared_aliases=list(matcher.prepared),
        parsed_cache=parsed_cache,
        ignore_arc_marker=learned_without_marker or (unmarked_ok and release_arc_qualifier(title) is None),
        allow_fuzzy=not matcher.strict,
    )
    if matched and matcher.season_known and release_season(title) != matcher.season:
        return False, parsed
    return matched, parsed


def decide_dated(
    matcher: Matcher,
    title: str,
    parsed_cache: Optional[ParsedCache],
    on_schedule: Callable[[int], bool],
) -> Tuple[bool, Dict[str, Any]]:
    """``decide`` for a release whose publish date is known.

    A show whose aliases name an arc ("2nd & 3rd STAGE") refuses releases that carry no arc
    marker, but groups usually do not write one for a split entry. A release published right
    after one of this show's own scheduled air times is on its schedule, which the numbering
    cannot tell us but the date can; ``on_schedule(raw_episode)`` says so, and the release is
    then accepted without the marker. A release that names a different arc, has no date or
    falls outside the schedule stays refused.
    """
    matched, parsed = decide(matcher, title, parsed_cache)
    if matched:
        return matched, parsed
    raw_episode = parsed.get("episode")
    if (
        raw_episode is not None
        and raw_episode > 0
        and alias_arc_requirement(list(matcher.aliases))
        and release_arc_qualifier(title) is None
        and on_schedule(int(raw_episode))
    ):
        return decide(matcher, title, parsed_cache, unmarked_ok=True)
    return matched, parsed


def newest_raw_episode(
    matcher: Matcher,
    titles: Iterable[str],
    parsed_cache: Optional[ParsedCache] = None,
) -> Optional[int]:
    """The highest episode number among the show's matching releases in a feed."""
    newest: Optional[int] = None
    for title in titles:
        if not title:
            continue
        matched, parsed = decide(matcher, title, parsed_cache)
        raw = parsed.get("episode")
        if matched and raw is not None and raw > 0 and (newest is None or raw > newest):
            newest = int(raw)
    return newest


def signature(entries: Iterable[Tuple[int, Sequence[str], str]]) -> int:
    """Changes whenever a show is added or removed, or its aliases or learned name change."""
    return hash(tuple(sorted((show_id, tuple(aliases), learned or "") for show_id, aliases, learned in entries)))


def matching_show_ids(
    matchers: Sequence[Tuple[int, Matcher]],
    title: str,
    parsed_cache: Optional[ParsedCache] = None,
    only: Optional[Iterable[int]] = None,
) -> List[int]:
    wanted = None if only is None else set(only)
    return [
        show_id for show_id, matcher in matchers
        if (wanted is None or show_id in wanted) and broad_match(matcher, title, parsed_cache)
    ]
