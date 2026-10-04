import logging
import os
import re
from datetime import timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError
from qbit_seasonal_anime.db.models import Feed, Monitored, MonitoredStatus, utc_now

logger = logging.getLogger("qbit_seasonal_anime.core.rules")


ROMAN_TO_INT = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}
INT_TO_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI"}
DEFAULT_MUST_NOT = r"(720p|480p|540p|360p|576p|batch|complete|\(\d+[-~]\d+\)|\[\d+[-~]\d+\])"

RELEASE_NAME_SPLIT_REGEX: re.Pattern[str] = re.compile(r"[\s._\-:–—/]+")
# At least one separator is required between tokens: the observed name had one
# everywhere we split, and requiring it keeps "Foo Bar" from matching "FooBar".
RELEASE_NAME_JOINER = r"[\s._\-:–—/]+"
MIN_PATTERN_TOKENS = 2
MIN_PATTERN_CHARS = 6


def generate_season_variants(alias: str) -> List[str]:
    """Generate common release group season abbreviations (e.g. 'Mushoku Tensei S3', 'Mushoku Tensei Season 3')."""
    variants = [alias]
    m_s = re.search(
        r"\b(?:season|s)\s*(\d+)\b|\b(\d+)(?:st|nd|rd|th)\s+season\b|\b(?:season|part)\s+(I|II|III|IV|V|VI)\b|\b(II|III|IV|V|VI)\b",
        alias,
        re.IGNORECASE,
    )
    if not m_s:
        return variants

    s_num = 0
    if m_s.group(1):
        s_num = int(m_s.group(1))
    elif m_s.group(2):
        s_num = int(m_s.group(2))
    elif m_s.group(3):
        s_num = ROMAN_TO_INT.get(m_s.group(3).upper(), 0)
    elif m_s.group(4):
        s_num = ROMAN_TO_INT.get(m_s.group(4).upper(), 0)

    if s_num == 0:
        return variants

    # Cut the season off the whole alias. Separators inside a title ("Re:Zero",
    # "Kaguya-sama", "Fate/stay night") are part of the name, so the base is never
    # truncated at one; only dangling ones left by the removal are trimmed.
    base = re.sub(
        r"\b(?:season|s)\s*\d+\b|\b\d+(?:st|nd|rd|th)\s+season\b|\b(?:season|part)\s+(?:I|II|III|IV|V|VI)\b|\b(II|III|IV|V|VI)\b",
        "",
        alias,
        flags=re.IGNORECASE,
    )
    base = re.sub(r"\s+", " ", base).strip()
    base = re.sub(r"^[\s:\-_–—/]+|[\s:\-_–—/]+$", "", base).strip()

    if base and len(base) >= 3:
        roman = INT_TO_ROMAN.get(s_num, "")
        s_tag = f"S0?{s_num}" if s_num < 10 else f"S{s_num}"
        variants.append(f"{base} {s_tag}")
        variants.append(f"{base} Season {s_num}")
        variants.append(f"{base} {s_num}")
        if roman:
            variants.append(f"{base} {roman}")
    return variants


def sanitize_regex_token(title: str) -> str:
    """Escape true regex metacharacters while keeping spaces and hyphens clean and human-readable."""
    if not title:
        return ""
    protected = title.replace("(0?)", "\x00ZERO_OPT_P\x00").replace("0?", "\x00ZERO_OPT\x00")
    escaped = re.sub(r"([\\^$.|?*+()\[\]{}])", r"\\\1", protected.strip())
    escaped = escaped.replace("\x00ZERO_OPT_P\x00", "(0?)").replace("\x00ZERO_OPT\x00", "0?")
    return re.sub(r"\s+", " ", escaped)


def build_release_name_pattern(release_name: str) -> str:
    """
    Build a separator-tolerant regex from a release name observed on a feed.

    Release groups are inconsistent about how they join the words of a title
    (spaces, dots, underscores, hyphens, colons), so a literal token stops
    matching as soon as the same show is announced differently. The episode
    number and quality tags are not part of the name, which keeps the pattern
    valid for every later episode of the same show.
    """
    if not release_name:
        return ""

    stripped = release_name.strip()
    tokens = [token for token in RELEASE_NAME_SPLIT_REGEX.split(stripped) if token]
    if not tokens:
        return ""

    if len(tokens) < MIN_PATTERN_TOKENS or len(stripped) < MIN_PATTERN_CHARS:
        return sanitize_regex_token(stripped)

    escaped = []
    for token in tokens:
        safe_token = sanitize_regex_token(token).replace("'", "['\u2019]?")
        if token.isdigit():
            # Bare numeric season variants ("Foo 3") must not match "Foo 30".
            safe_token += r"\b"
        escaped.append(safe_token)
    return RELEASE_NAME_JOINER.join(escaped)


def build_regex_pattern(
    aliases: List[str],
    matched_title: Optional[str] = None,
    release_group: Optional[str] = None,
) -> str:
    """
    Build a case-insensitive qBittorrent RSS rule pattern.
    If matched_title is known, use it as the rule; otherwise build an alternation from aliases.
    ``release_group`` is accepted for call compatibility but does not affect the pattern.
    """
    if matched_title and matched_title.strip():
        # The release group has already shown its naming, so the learned rule is
        # exactly that name. Season hedges ("S2", "Season 2", "II") belong on the
        # armed path below, where the naming is still unknown.
        return build_release_name_pattern(matched_title) or ".*"

    valid_aliases = [a.strip() for a in aliases if a and a.strip()]
    latin_aliases = [a for a in valid_aliases if any(c.isascii() and c.isalnum() for c in a)]
    target_aliases = latin_aliases if latin_aliases else valid_aliases

    if not target_aliases:
        return ".*"

    expanded_aliases: List[str] = []
    for a in target_aliases:
        expanded_aliases.extend(generate_season_variants(a))

    # A variant ending in a number ("Foo 3", "Foo S03") must not match a longer
    # number ("Foo 30"). A digit lookahead rather than \b, so "Foo S03E01" still
    # matches; it is appended after escaping so it stays a regex.
    tokens = [
        sanitize_regex_token(a) + (r"(?!\d)" if a.strip()[-1:].isdigit() else "")
        for a in expanded_aliases
    ]
    unique_tokens = list(dict.fromkeys(tokens))
    alternation = "|".join(unique_tokens)

    return rf"({alternation})"


def sanitize_folder_name(name: str) -> str:
    r"""
    Clean and sanitize anime title into a safe, valid, cross-platform folder name.
    - Replaces slashes between words or fractions (e.g. 'Ranma 1/2' -> 'Ranma 1-2', 'Fate/stay' -> 'Fate-stay')
    - Replaces remaining slashes and backslashes with ' - '
    - Replaces colons with ' - '
    - Removes forbidden filesystem characters: < > : " / \ | ? * and control chars
    - Collapses multiple hyphens and whitespace
    - Strips leading/trailing dots and spaces
    """
    if not name:
        return "Anime"
    s = re.sub(r"(\w+)[/](\w+)", r"\1-\2", name)
    s = re.sub(r"[/\\|]", " - ", s)
    s = re.sub(r":\s*", " - ", s)
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s)
    s = re.sub(r"\s*-\s*-\s*", " - ", s)
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .")
    return s or "Anime"


def compress_home_path(path: Optional[str]) -> str:
    """
    If a path is within the user's home directory, collapses the absolute home prefix into '~' for clean UI display.
    Works seamlessly across Linux (/home/username -> ~) and Windows (C:\\Users\\username -> ~).
    """
    if not path:
        return ""
    try:
        home = str(Path.home())
        p_str = str(path).strip()
        if p_str == home:
            return "~"
        if p_str.startswith(home + "/") or p_str.startswith(home + "\\"):
            return "~" + p_str[len(home):]
    except Exception:
        pass
    return str(path)


def resolve_save_path(base_dir: str, display_name: str, custom_save_folder: Optional[str] = None) -> str:
    """
    Resolves the final absolute filesystem save path for an anime show.
    If custom_save_folder is provided, uses it (expanding ~, {name} if present, or resolving relative names).
    Otherwise, applies display_name into base_dir template.
    If base_dir is blank and no custom path is set, returns empty string "" so qBittorrent uses its default download location.
    """
    clean_name = sanitize_folder_name(display_name)

    if custom_save_folder and custom_save_folder.strip():
        raw = custom_save_folder.strip()
        if "{name}" in raw:
            raw = raw.replace("{name}", clean_name)
        elif not raw.startswith("/") and not raw.startswith("~") and not (len(raw) > 2 and raw[1] == ":"):
            # If user provided a relative subfolder (e.g. "Bleach Season 2")
            base_template = base_dir.strip() if base_dir and base_dir.strip() else ""
            if base_template:
                base_prefix = base_template.replace("{name}", "").rstrip("/\\") if "{name}" in base_template else base_template.rstrip("/\\")
                raw = f"{base_prefix}/{raw}"
            else:
                raw = clean_name
        return str(Path(os.path.expanduser(raw)).resolve()) if (raw.startswith("/") or raw.startswith("~") or (len(raw) > 2 and raw[1] == ":")) else raw

    if not base_dir or not base_dir.strip():
        return ""

    base_template = base_dir.strip()
    if "{name}" in base_template:
        expanded = base_template.replace("{name}", clean_name)
    else:
        expanded = str(Path(base_template) / clean_name)

    return str(Path(os.path.expanduser(expanded)).resolve())


def build_rule_name(monitored_id: int, display_name: str) -> str:
    """Construct a clean, human-readable rule name for qBittorrent without ID clutter."""
    clean_name = sanitize_folder_name(display_name)
    return f"[Seasonal] {clean_name}"


def is_show_rule_unreleased(monitored: Monitored) -> bool:
    """
    True while a show has not started airing yet: premiere still ahead (or unscheduled)
    and nothing downloaded so far. These shows are "upcoming", not "testing".
    """
    now = utc_now()
    air_at = monitored.next_airing_at
    if air_at and air_at.tzinfo is None:
        air_at = air_at.replace(tzinfo=timezone.utc)

    return (
        (monitored.next_airing_episode == 1 or monitored.next_airing_episode is None)
        and (air_at is None or air_at > now)
        and (monitored.last_confirmed_episode or 0) == 0
    )


def is_show_rule_enabled(monitored: Monitored) -> bool:
    """
    Determine if a show's RSS rule in qBittorrent should be actively enabled.
    - Paused or Completed shows: False
    - Fixed (Working / Confirmed) shows: True
    - Unconfirmed upcoming shows whose air date is in the future: False (prevents pre-air false positives)
    - Unconfirmed shows that have aired / are in hunting mode: True
    """
    if monitored.status in (MonitoredStatus.PAUSED, MonitoredStatus.COMPLETED):
        return False

    if monitored.status == MonitoredStatus.FIXED:
        return True

    if is_show_rule_unreleased(monitored):
        return False

    return True


def build_rule_definition(
    monitored: Monitored,
    feed_url: str,
    base_dir: str,
    category: str = "",
    ratio_limit: float = 1.0,
    release_group: Optional[str] = None,
    enabled: Optional[bool] = None,
    must_contain: Optional[str] = None,
    must_not_contain: Optional[str] = None,
    title_language: str = "english",
    previous_rule: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Construct the JSON payload for qBittorrent's RSS rule definition.

    Pass the rule as it currently exists in qBittorrent as ``previous_rule`` so
    state qBittorrent owns (when it last matched, which episodes it already has)
    is carried over instead of being reset by every write.
    """
    effective_group = release_group or monitored.matched_release_group
    previous_state = previous_rule or {}
    
    regex = (
        must_contain
        if must_contain is not None
        else (
            getattr(monitored, "custom_regex", None)
            or build_regex_pattern(
                monitored.effective_aliases,
                matched_title=monitored.matched_title,
                release_group=effective_group,
            )
        )
    )

    effective_display_name = (
        monitored.title_english
        if (title_language == "english" and monitored.title_english)
        else (monitored.title_romaji or monitored.display_name)
    )
    save_path = resolve_save_path(base_dir, effective_display_name, monitored.save_folder)

    effective_must_not = (
        must_not_contain
        if must_not_contain is not None
        else (getattr(monitored, "custom_must_not", None) or DEFAULT_MUST_NOT)
    )

    is_enabled = is_show_rule_enabled(monitored) if enabled is None else enabled

    rule_def = {
        "enabled": is_enabled,
        "mustContain": regex,
        "mustNotContain": effective_must_not,
        "useRegex": True,
        "episodeFilter": "",
        "smartFilter": False,
        "previouslyMatchedEpisodes": list(previous_state.get("previouslyMatchedEpisodes") or []),
        "affectedFeeds": [feed_url],
        "ignoreDays": 0,
        "lastMatch": previous_state.get("lastMatch") or "",
        "addPaused": False,
        "assignedCategory": category,
        "savePath": save_path,
        "ratioLimit": ratio_limit,
        "torrentParams": {
            "category": category,
            "save_path": save_path,
            "ratio_limit": ratio_limit,
            "operating_mode": "AutoManaged",
        },
    }
    return rule_def


def create_or_update_rule(
    qbit_client: QBitClient,
    monitored: Monitored,
    feed: Feed,
    base_dir: str,
    category: str = "",
    ratio_limit: float = 1.0,
    release_group: Optional[str] = None,
    enabled: Optional[bool] = None,
    must_contain: Optional[str] = None,
    must_not_contain: Optional[str] = None,
    title_language: str = "english",
    known_categories: Optional[Set[str]] = None,
    previous_rule: Optional[Dict[str, Any]] = None,
) -> str:
    """Create or update a qBittorrent RSS rule and return the rule name."""
    if category and (known_categories is None or category not in known_categories):
        if qbit_client.ensure_category_exists(category) and known_categories is not None:
            known_categories.add(category)

    rule_name = monitored.qbit_rule_name or build_rule_name(monitored.id or 0, monitored.display_name)

    if previous_rule is None:
        try:
            previous_rule = qbit_client.get_rss_rules().get(rule_name)
        except QbitClientError as e:
            logger.debug(f"Could not read existing rule '{rule_name}' state: {e}")
            previous_rule = None

    rule_def = build_rule_definition(
        monitored=monitored,
        feed_url=feed.qbit_feed_url,
        base_dir=base_dir,
        category=category,
        ratio_limit=ratio_limit,
        release_group=release_group,
        enabled=enabled,
        must_contain=must_contain,
        must_not_contain=must_not_contain,
        title_language=title_language,
        previous_rule=previous_rule,
    )

    qbit_client.set_rss_rule(rule_name=rule_name, rule_def=rule_def)

    if logger.isEnabledFor(logging.DEBUG):
        try:
            matched = qbit_client.get_matching_articles(rule_name)
            match_count = sum(len(v) for v in matched.values()) if isinstance(matched, dict) else 0
            logger.debug(f"Rule '{rule_name}' sanity check: qBittorrent matched {match_count} article(s).")
        except Exception as e:
            logger.debug(f"Rule '{rule_name}' matching articles check skipped: {e}")

    return rule_name


def delete_rule(qbit_client: QBitClient, rule_name: str, *, raise_on_error: bool = False) -> None:
    """Remove a rule from qBittorrent.

    A failure is logged and swallowed by default, because most callers are on a
    cleanup path where a leftover rule is not worth aborting for. Callers that
    would be left with an orphaned rule still downloading pass
    ``raise_on_error`` so they can refuse to continue.
    """
    if not rule_name:
        return
    try:
        qbit_client.remove_rss_rule(rule_name=rule_name)
    except QbitClientError as e:
        if raise_on_error:
            raise
        logger.warning(f"Could not delete rule '{rule_name}': {e}")


def disable_rule(qbit_client: QBitClient, rule_name: str, *, raise_on_error: bool = False) -> None:
    """Disable an RSS auto-downloading rule in qBittorrent without deleting it."""
    if not rule_name:
        return
    try:
        rules = qbit_client.get_rss_rules()
        if rule_name in rules:
            rdef = rules[rule_name]
            if rdef.get("enabled") is not False:
                rdef["enabled"] = False
                qbit_client.set_rss_rule(rule_name=rule_name, rule_def=rdef)
                logger.info(f"Disabled RSS rule '{rule_name}' in qBittorrent.")
    except Exception as e:
        if raise_on_error:
            raise
        logger.debug(f"Could not disable rule '{rule_name}': {e}")
