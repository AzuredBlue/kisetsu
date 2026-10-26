import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

ROMAN_TO_INT = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}
INT_TO_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI"}
MIN_PATTERN_CHARS = 6
# Release groups join the words of a title with spaces, dots, underscores or hyphens
# (a feed title and the torrent's file name often differ), so matching ignores which.
NAME_SEPARATORS = r"[\s._\-:–—/]+"
_NAME_SPLIT = re.compile(NAME_SEPARATORS)

_SEASON_MARK = re.compile(
    r"\b(?:season|s)\s*(\d+)\b|\b(\d+)(?:st|nd|rd|th)\s+season\b|\b(?:season|part)\s+(I|II|III|IV|V|VI)\b|\b(II|III|IV|V|VI)\b",
    re.IGNORECASE,
)


def generate_season_variants(alias: str) -> List[str]:
    """Generate common release group season abbreviations (e.g. 'Mushoku Tensei S3', 'Mushoku Tensei Season 3')."""
    variants = [alias]
    m_s = _SEASON_MARK.search(alias)
    if not m_s:
        return variants

    # Whichever alternative matched is the only group that captured anything.
    token = next(group for group in m_s.groups() if group)
    s_num = int(token) if token.isdigit() else ROMAN_TO_INT.get(token.upper(), 0)

    if s_num == 0:
        return variants

    # Cut the season off the whole alias. Separators inside a title ("Re:Zero",
    # "Kaguya-sama", "Fate/stay night") are part of the name, so the base is never
    # truncated at one; only dangling ones left by the removal are trimmed.
    base = _SEASON_MARK.sub("", alias)
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


def name_end_guard(name: str) -> str:
    """The lookahead that keeps a name's trailing season number from matching a longer one.

    "Foo 3" must not match "Foo 30" and "Foo II" must not match "Foo III", but
    "Foo 3 - 01" and "Foo S03E01" still match.
    """
    stripped = (name or "").strip()
    if stripped[-1:].isdigit():
        return r"(?!\d)"
    words = stripped.split()
    if len(words) > 1 and words[-1].upper() in ROMAN_TO_INT:
        return r"(?![A-Za-z0-9])"
    return ""


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
    Build a regex from a release name observed on a feed.

    The name already showed how the release group writes the title, so the
    pattern is that name with only regex metacharacters escaped. The episode
    number and quality tags are not part of the name, which keeps the pattern
    valid for every later episode of the same show.
    """
    stripped = (release_name or "").strip()
    if not stripped:
        return ""
    return sanitize_regex_token(stripped) + name_end_guard(stripped)


def build_tolerant_name_pattern(name: str) -> str:
    """A regex for a series name that matches whichever separators a release uses.

    Only the words are fixed; "Blue Box S02", "Blue.Box.S02" and "Blue_Box_S02"
    all match, while a missing separator ("BlueBox") does not.
    """
    stripped = (name or "").strip()
    tokens = [token for token in _NAME_SPLIT.split(stripped) if token]
    if not tokens:
        return ""
    if len(tokens) < 2:
        return build_release_name_pattern(stripped)
    escaped = []
    for token in tokens:
        safe = sanitize_regex_token(token).replace("'", "['\u2019]?")
        if token.isdigit():
            # "Foo 3" must not match "Foo 30".
            safe += r"\b"
        escaped.append(safe)
    pattern = NAME_SEPARATORS.join(escaped)
    if not tokens[-1].isdigit():
        pattern += name_end_guard(" ".join(tokens))
    return pattern


def show_match_patterns(aliases: List[str], matched_title: Optional[str]) -> Tuple[str, Optional[str]]:
    """(pattern that finds a show's releases, the learned-name pattern or None).

    The learned name is how the release group writes the title, so it is tried
    alongside the aliases.
    """
    pattern = build_regex_pattern(aliases)
    learned = (matched_title or "").strip()
    if len(learned) < MIN_PATTERN_CHARS:
        return pattern, None
    learned_pattern = build_tolerant_name_pattern(learned)
    return f"{learned_pattern}|{pattern}", learned_pattern


def build_regex_pattern(
    aliases: List[str],
    matched_title: Optional[str] = None,
) -> str:
    """
    Build a case-insensitive qBittorrent RSS rule pattern.
    If matched_title is known, use it as the rule; otherwise build an alternation from aliases.
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

    # A variant ending in a season number ("Foo 3", "Foo S03", "Foo II") must not
    # match a longer one ("Foo 30", "Foo III"). A lookahead rather than \b, so
    # "Foo S03E01" still matches; it is appended after escaping so it stays a regex.
    tokens = [sanitize_regex_token(a) + name_end_guard(a) for a in expanded_aliases]
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
    except (RuntimeError, KeyError, OSError):
        return str(path)
    p_str = str(path).strip()
    if p_str == home:
        return "~"
    if p_str.startswith(home + "/") or p_str.startswith(home + "\\"):
        return "~" + p_str[len(home):]
    return str(path)


def effective_title(show, title_language: str = "english") -> str:
    """The show's name in the configured language, falling back to romaji and then display_name."""
    if title_language == "english" and show.title_english:
        return show.title_english
    return show.title_romaji or show.display_name


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
