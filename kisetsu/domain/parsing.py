import logging
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple
from guessit import guessit
from rapidfuzz import fuzz
from kisetsu.config import FUZZY_MATCH_THRESHOLD
from kisetsu.domain.patterns import build_regex_pattern

logger = logging.getLogger("kisetsu.domain.parsing")


VERSION_REGEX: re.Pattern[str] = re.compile(r"(E\d+|\b\d+)v\d+", re.IGNORECASE)
GUESSIT_OPTIONS: Dict[str, Any] = {"excludes": ["country", "language"]}


STOPWORDS: set = {"the", "a", "an", "no", "wa", "ga", "to", "de", "ni", "la", "le", "el"}


def prepare_aliases(aliases: List[str]) -> List[Tuple[str, str]]:
    prepared = []
    for alias in aliases:
        if not alias:
            continue
        normalized = normalize_title(alias)
        if normalized:
            prepared.append((alias, normalized))
    return prepared


_ROMAN_NUMERALS = {"x": "10", "ix": "9", "viii": "8", "vii": "7", "vi": "6", "v": "5", "iv": "4", "iii": "3", "ii": "2"}
_ROMAN_NUMERAL_RE = re.compile(r"\b(" + "|".join(_ROMAN_NUMERALS) + r")\b")


@lru_cache(maxsize=4096)
def normalize_title(title: str) -> str:
    """Normalize anime title for comparison by standardizing seasons, roman numerals, and symbols."""
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"\[.*?\]|\(.*?\)", " ", t)

    t = _ROMAN_NUMERAL_RE.sub(lambda m: _ROMAN_NUMERALS[m.group(1)], t)
    t = re.sub(r"\b(\d+)(?:st|nd|rd|th)\s+season\b", r"season \1", t)
    t = re.sub(r"\bs(\d+)\b", r"season \1", t)

    t = re.sub(r"[:\-_/\\.|+~'\"`]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


NON_GROUP_TAGS = {
    "1080p", "720p", "480p", "540p", "576p", "2160p", "4k",
    "hevc", "x264", "x265", "avc", "h264", "h265", "aac", "flac", "opus",
    "web-dl", "webrip", "hdtv", "dvd", "bd", "bluray", "cr",
    "multisub", "multi-sub", "multi-subs", "multisubs", "eng", "english",
    "raw", "raws", "sub", "subs", "dub", "dual-audio", "batch",
}


def extract_release_group_tag(raw_title: str) -> Optional[str]:
    """Extract release group tag whether at start [SubsPlease], at end [Varyg], or scene hyphen -VARYG."""
    m_start = re.match(r"^\[(.*?)\]", raw_title.strip())
    if m_start:
        cand = m_start.group(1).strip()
        if cand.lower() not in NON_GROUP_TAGS and not cand.isdigit():
            return cand

    m_scene = re.search(r"-([A-Za-z0-9_]+)(?:\s*\(|\s*\[|\s*\.|\s*$)", raw_title)
    if m_scene:
        cand = m_scene.group(1).strip()
        if cand.lower() not in NON_GROUP_TAGS and len(cand) >= 2 and not cand.isdigit():
            return cand

    brackets = re.findall(r"\[(.*?)\]", raw_title)
    if brackets:
        for b in reversed(brackets):
            val = b.strip()
            if re.fullmatch(r"[0-9A-Fa-f]{8}", val) or re.fullmatch(r"[0-9A-Fa-f]{4}", val):
                continue
            if val.lower() in NON_GROUP_TAGS or val.isdigit():
                continue
            return val
    return None


def extract_release_version(raw_title: str) -> int:
    if not raw_title:
        return 1
    match = re.search(
        r"(?:E\d+|\b\d+)v(\d+)\b|[\[(]v(\d+)[\])]|\b(?:v|ver\.?|version)\s*(\d+)\b",
        raw_title,
        re.IGNORECASE,
    )
    if match:
        value = match.group(1) or match.group(2) or match.group(3)
        if value and value.isdigit():
            return int(value)
    if re.search(r"\bREPACK\d*\b|\bPROPER\d*\b", raw_title, re.IGNORECASE):
        repack = re.search(r"\bREPACK(\d+)\b", raw_title, re.IGNORECASE)
        return 1 + int(repack.group(1)) if repack else 2
    return 1


def parse_release_title(raw_title: str) -> Dict[str, Any]:
    """Parse torrent release filename/title into structured metadata."""
    if not raw_title:
        return {
            "raw_title": "",
            "title": "",
            "episode": None,
            "season": None,
            "release_group": None,
            "version": 1,
        }

    version = extract_release_version(raw_title)
    release_group = extract_release_group_tag(raw_title)
    cleaned = raw_title.strip()

    if release_group:
        cleaned = re.sub(rf"^\[{re.escape(release_group)}\]\s*", "", cleaned)
        cleaned = re.sub(rf"\[{re.escape(release_group)}\]\s*", "", cleaned)
        cleaned = re.sub(rf"-{re.escape(release_group)}\b\s*", "", cleaned)

    cleaned = VERSION_REGEX.sub(r"\1", cleaned)

    cleaned = re.sub(r"\.(?:mkv|mp4|avi|webm|mov|m4v|ts)$", "", cleaned, flags=re.IGNORECASE).strip()

    cleaned = re.sub(
        r"\b(?:DDP?|EAC-?3|AC3|AAC|FLAC|OPUS|DTS(?:-HD)?|TrueHD|PCM|DD\+?|Dolby|Atmos)?\s*(?:[1-7]\.[0-2]|2\.0|5\.1|7\.1)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\b\d+(?:\.\d+)?\s*fps\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b\d{1,2}\s*-?\s*bits?\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\b(?:2160p|1080p|1080i|720p|576p|540p|480p|4k|8k|UHD|FHD|HD|WEB-?DL|WEBRip|BDRip|BluRay|BD|HDTV|DVD(?:Rip)?|REMUX|Dual-Audio|Multi-Audio|Multi-Subs?|MultiSub|Uncensored|Batch|Complete)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )

    pipe_split = cleaned.split("|")
    primary_segment = pipe_split[0].strip()

    episode_num = None
    season_num = None

    m_dual = re.search(r"-\s*(\d{1,4})\s*\(\s*S(\d{1,2})E(\d{1,4})\s*\)", primary_segment, re.IGNORECASE)
    if m_dual:
        episode_num = int(m_dual.group(1))
        season_num = int(m_dual.group(2))
        title_candidate = primary_segment[:m_dual.start()].strip()
    else:
        m_se = re.search(r"\bS(\d{1,2})\s*[-_.]?\s*E(\d{1,4})\b", primary_segment, re.IGNORECASE)
        if m_se:
            season_num = int(m_se.group(1))
            episode_num = int(m_se.group(2))
            title_candidate = primary_segment[:m_se.start()].strip()
        else:
            m_s_ep = re.search(r"\b(?:S|Season\s*)(\d{1,2})\s*-\s*(\d{1,4})\b", primary_segment, re.IGNORECASE)
            if m_s_ep:
                season_num = int(m_s_ep.group(1))
                episode_num = int(m_s_ep.group(2))
                title_candidate = primary_segment[:m_s_ep.start()].strip()
            else:
                # Exclude 4-digit years from standalone episode matching unless prefixed by EP/E/#
                ep_matches = list(re.finditer(
                    r"(?:\s*-\s*|\s+)(?:EP?|#)?(\d{1,4})(?:\s*\(|\s*\[|\s*\.|\s*$)",
                    primary_segment,
                    re.IGNORECASE,
                ))
                valid_ep_matches = []
                for m in ep_matches:
                    val = int(m.group(1))
                    is_prefixed = bool(re.match(r"(?:EP?|#)", m.group(0).strip(" -_")))
                    if 1950 <= val <= 2050 and not is_prefixed:
                        continue
                    valid_ep_matches.append(m)

                if valid_ep_matches:
                    m_ep = valid_ep_matches[-1]
                    episode_num = int(m_ep.group(1))
                    title_candidate = primary_segment[:m_ep.start()].strip()
                else:
                    m_s_only = re.search(r"\b(?:S|Season\s*)(\d{1,2})\b", primary_segment, re.IGNORECASE)
                    if m_s_only:
                        season_num = int(m_s_only.group(1))
                    title_candidate = primary_segment

    title_candidate = re.sub(r"\[.*?\]|\(.*?\)", "", title_candidate)
    title_candidate = re.sub(r"\b(?:19|20)\d{2}\b", "", title_candidate)
    trail_tags = r"\b(?:2160p|1080p|1080i|720p|576p|540p|480p|4k|8k|UHD|FHD|HD|WEB-?DL|WEBRip|BDRip|BluRay|BD|HDTV|DVD(?:Rip)?|REMUX|AVC|HEVC|x264|x265|H\.?264|H\.?265|AV1|VP9|AAC|FLAC|OPUS|DDP?|EAC3|AC3|TrueHD|PCM|Dual-Audio|Multi-Audio|Multi-Subs?|MultiSub|DUAL|NF|CR|AMZN|HIDIVE|BILI|REPACK\d?|PROPER|Uncensored|Batch|Complete)\b"
    for _ in range(5):
        title_candidate = re.sub(trail_tags + r"[\s\-_|/:\(\[\{.]*$", "", title_candidate, flags=re.IGNORECASE).strip()
        title_candidate = re.sub(r"[\s\-_|/:\(\[\{.]+$", "", title_candidate).strip()

    title_candidate = re.sub(r"^[\s\-_|/:\)\]\}\.]+", "", title_candidate).strip()
    title_candidate = re.sub(r"\s+", " ", title_candidate).strip()

    if title_candidate:
        return {
            "raw_title": raw_title,
            "title": title_candidate,
            "episode": episode_num,
            "season": season_num,
            "release_group": release_group,
            "version": version,
        }

    try:
        clean_for_guessit = re.sub(r"(?: - Movie)|[\\/:!*?\"<>|._-](?!\s*\d)", " ", raw_title)
        clean_for_guessit = " ".join(clean_for_guessit.split())
        v_match = VERSION_REGEX.search(clean_for_guessit)
        if v_match:
            clean_for_guessit = clean_for_guessit.replace(v_match.group(0), v_match.group(1))

        guess = dict(guessit(clean_for_guessit, options=GUESSIT_OPTIONS))
    except Exception as e:
        logger.debug(f"Guessit failed to parse '{raw_title}': {e}")
        guess = {}

    g_episode = guess.get("episode")
    g_season = guess.get("season", "")
    g_part = str(guess.get("part", ""))
    remaining: List[int] = []

    if guess.get("episode_title", "").isdigit() and "episode" not in guess:
        g_episode = int(guess.get("episode_title"))

    if isinstance(g_episode, list):
        remaining = g_episode[:-1]
        g_episode = g_episode[-1]

    if isinstance(g_season, list):
        if g_episode is None and len(g_season) > 1:
            g_episode = g_season[-1]
        g_season = g_season[0]

    guessed_name = str(guess.get("title") or title_candidate or raw_title)
    if remaining:
        guessed_name += " " + " ".join(str(ep) for ep in remaining)
    if g_season and str(g_season).isdigit() and int(g_season) > 1 and f"Season {g_season}" not in guessed_name:
        guessed_name += f" Season {g_season}"
    if g_part and f"Part {g_part}" not in guessed_name:
        guessed_name += f" Part {g_part}"

    return {
        "raw_title": raw_title,
        "title": guessed_name,
        "episode": episode_num or g_episode,
        "season": season_num or (int(g_season) if str(g_season).isdigit() else None),
        "release_group": release_group or guess.get("release_group"),
        "version": version,
    }


def calculate_match_score(
    parsed_title: str,
    aliases: List[str],
    prepared_aliases: Optional[List[Tuple[str, str]]] = None,
) -> Tuple[float, Optional[str]]:
    """
    Calculate maximum fuzzy match score against a list of aliases.
    Returns (highest_score, best_matching_alias).
    """
    if not parsed_title or not aliases:
        return 0.0, None

    norm_parsed = normalize_title(parsed_title)
    if not norm_parsed:
        return 0.0, None

    tokens_parsed = [w for w in norm_parsed.split() if w not in STOPWORDS]
    if not tokens_parsed:
        return 0.0, None

    best_score = 0.0
    best_alias = None

    if prepared_aliases is None:
        prepared_aliases = prepare_aliases(aliases)

    for alias, norm_alias in prepared_aliases:

        if norm_parsed == norm_alias:
            return 100.0, alias

        score = float(fuzz.token_set_ratio(norm_parsed, norm_alias))

        if len(tokens_parsed) == 1 and len(tokens_parsed[0]) < 5:
            score = float(fuzz.token_sort_ratio(norm_parsed, norm_alias))

        if score > best_score:
            best_score = score
            best_alias = alias

    return best_score, best_alias


UNWANTED_RESOLUTION_REGEX: re.Pattern[str] = re.compile(r"\b(720p|480p|540p|360p|576p)\b", re.IGNORECASE)
BATCH_REGEX: re.Pattern[str] = re.compile(
    r"\b(batch|complete\s+series|complete\s+season|collection)\b|\(\d+\s*[-~]\s*\d+\)|\[\d+\s*[-~]\s*\d+\]|\b\d{1,4}\s*~\s*\d{1,4}\b",
    re.IGNORECASE,
)


def is_valid_release(raw_title: str) -> bool:
    """Reject batches and unwanted lower-resolution releases."""
    if UNWANTED_RESOLUTION_REGEX.search(raw_title):
        return False
    if BATCH_REGEX.search(raw_title):
        return False
    return True


def extract_arc_qualifiers(text: str) -> Optional[str]:
    """Normalize arc/cours/part markers so split seasons stay distinguishable.

    Returns a canonical token such as ``stage1``, ``stage2``, ``part7`` or
    ``season4``, or ``None`` when the text carries no arc marker at all.
    """
    if not text:
        return None
    lowered = text.lower()
    stage = re.search(r"(\d+)\s*(?:st|nd|rd|th)?\s*(?:-\s*\d+\s*(?:st|nd|rd|th)?\s*)?stage", lowered)
    if stage:
        numbers = re.findall(r"\d+", stage.group(0))
        if numbers:
            return "stage" + "-".join(numbers)
    part = re.search(r"part\s*(\d+)", lowered)
    if part:
        return f"part{part.group(1)}"
    season = re.search(r"season\s*(\d+)", lowered)
    if season:
        return f"season{season.group(1)}"
    return None


_EPISODE_TOKEN = re.compile(r"\bS\d{1,2}\s*[-_.]?\s*E\d{1,4}\b|\s-\s*\d{1,4}(?:v\d+)?(?=[\s\[(.]|$)", re.IGNORECASE)


def release_arc_qualifier(raw_title: str) -> Optional[str]:
    """The arc marker in a release's series name, ignoring what follows the episode number.

    Episode names, quality tags and alternate-title blocks come after the episode
    number and can mention a "Part 5" or "Part 1080p" that is not the show's arc.
    """
    token = _EPISODE_TOKEN.search(raw_title or "")
    return extract_arc_qualifiers(raw_title[:token.start()] if token else raw_title)


_ROMAN_SEASONS = {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}
_SEASON_NAME_PATTERNS = (
    re.compile(r"\bseason\s*(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+season\b", re.IGNORECASE),
    re.compile(r"\bS(\d{1,2})\b", re.IGNORECASE),
)
_SEASON_TAG = re.compile(r"\bS(\d{1,2})\s*[-_.]?\s*E\d{1,4}\b", re.IGNORECASE)
_TRAILING_ROMAN = re.compile(r"\s(ii|iii|iv|v|vi)\s*$", re.IGNORECASE)


def _series_name_part(raw_title: str) -> str:
    """The series name of a release: no leading group tag, nothing from the episode number on."""
    title = raw_title or ""
    token = _EPISODE_TOKEN.search(title)
    name = title[:token.start()] if token else title
    return re.sub(r"^\s*\[[^\]]*\]\s*", "", name)


def _name_season(name: str) -> Optional[int]:
    for pattern in _SEASON_NAME_PATTERNS:
        match = pattern.search(name)
        if match:
            return int(match.group(1))
    roman = _TRAILING_ROMAN.search(name)
    return _ROMAN_SEASONS[roman.group(1).lower()] if roman else None


def release_season(raw_title: str) -> Optional[int]:
    """The season a release names for itself, or None when it names none.

    Reads the series name in front of the episode number ("Link Click S3",
    "Foo 2nd Season", "Mob Psycho 100 III"), then an "S03E05" tag.
    """
    season = _name_season(_series_name_part(raw_title))
    if season is not None:
        return season
    tag = _SEASON_TAG.search(raw_title or "")
    return int(tag.group(1)) if tag else None


def alias_arc_requirement(aliases: List[str]) -> Optional[str]:
    """The arc marker a show's aliases insist on, if any.

    Only cours/stage and part markers are enforced: release groups routinely
    omit a plain "Season N" marker, but a JoJo-style cour split is exactly the
    case where a bare title is ambiguous between arcs.
    """
    for alias in aliases:
        qualifier = extract_arc_qualifiers(alias)
        if qualifier and (qualifier.startswith("stage") or qualifier.startswith("part")):
            return qualifier
    return None


def _arc_is_compatible(show_requirement: str, title_qualifier: Optional[str]) -> bool:
    if title_qualifier is None:
        return False
    if show_requirement == title_qualifier:
        return True
    # "stage2-3" accepts a title that names either half of that cour pair.
    if show_requirement.startswith("stage") and title_qualifier.startswith("stage"):
        return set(show_requirement[5:].split("-")) & set(title_qualifier[5:].split("-")) != set()
    return False


def _keep_season_in_title(raw_title: str, parsed: Dict[str, Any]) -> None:
    """Make the learned title carry the season marker the release writes in its name ("Ao Ashi S2")."""
    name = _series_name_part(raw_title)
    if _name_season(name) is None or _name_season(parsed.get("title") or "") is not None:
        return
    name = re.sub(r"[\s\-_|/:]+$", "", name).strip()
    if name:
        parsed["title"] = name


def match_release_to_show(
    raw_title: str,
    aliases: List[str],
    threshold: float = FUZZY_MATCH_THRESHOLD,
    test_pattern: Optional[str] = None,
    prepared_aliases: Optional[List[Tuple[str, str]]] = None,
    parsed_cache: Optional[Dict[str, Dict[str, Any]]] = None,
    ignore_arc_marker: bool = False,
    allow_fuzzy: bool = True,
) -> Tuple[bool, float, Dict[str, Any]]:
    """
    Evaluate if an RSS item or torrent matches an anime show.
    Rejects batches and lower resolutions, then matches against the show's testing regex or aliases.
    Returns (is_match, score, parsed_metadata).

    ``ignore_arc_marker`` is for callers that only interpret an already-downloaded
    release. Callers that choose which torrents to download leave it ``False`` so a
    release belonging to a different cour of the same arc is never claimed.
    """
    if parsed_cache is not None and raw_title in parsed_cache:
        parsed = dict(parsed_cache[raw_title])
    else:
        parsed = parse_release_title(raw_title)
        if parsed_cache is not None:
            parsed_cache[raw_title] = dict(parsed)
    if not is_valid_release(raw_title):
        return False, 0.0, parsed

    requirement = None if ignore_arc_marker else alias_arc_requirement(aliases)
    if requirement and not _arc_is_compatible(requirement, release_arc_qualifier(raw_title)):
        logger.debug(
            f"Rejected '{raw_title}': show requires arc marker '{requirement}'."
        )
        return False, 0.0, parsed

    if test_pattern is None:
        test_pattern = build_regex_pattern(aliases)
    try:
        m = re.search(test_pattern, raw_title, flags=re.IGNORECASE)
        if m:
            matched_token = m.group(0).strip()
            if matched_token:
                parsed["title"] = matched_token
            _keep_season_in_title(raw_title, parsed)
            return True, 100.0, parsed
    except re.error as e:
        logger.warning(f"Invalid match pattern for '{raw_title}': {e}")

    if not allow_fuzzy:
        return False, 0.0, parsed

    parsed_title = parsed.get("title", "")
    score, best_alias = calculate_match_score(
        parsed_title,
        aliases,
        prepared_aliases=prepared_aliases,
    )
    is_match = score >= threshold
    if is_match:
        _keep_season_in_title(raw_title, parsed)
    return is_match, score, parsed


def release_key(name: str) -> str:
    """A release name reduced to its words, so naming styles compare equal.

    A feed title ("Blue Box S02E01 … H.264-VARYG (Ao no Hako …)") and the
    torrent's file name ("Blue.Box.S02E01.….H.264-VARYG.mkv") name one release.
    """
    key = re.sub(r"\.(mkv|mp4|avi)$", "", (name or "").strip(), flags=re.IGNORECASE)
    return re.sub(r"[^0-9a-z]+", " ", key.lower()).strip()


def same_release(first: str, second: str) -> bool:
    """Whether two names are one release: equal words, or one extends the other by whole words."""
    a, b = release_key(first), release_key(second)
    if not a or not b:
        return False
    return a == b or a.startswith(b + " ") or b.startswith(a + " ")
