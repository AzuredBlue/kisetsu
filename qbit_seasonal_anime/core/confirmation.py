from datetime import datetime, timezone, timedelta
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from sqlmodel import Session, select, or_
from rapidfuzz import fuzz
from qbit_seasonal_anime.config import DEFAULT_DOWNLOAD_MODE
from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError
from qbit_seasonal_anime.core.matching import (
    match_release_to_show,
    normalize_title,
    parse_release_title,
    prepare_aliases,
)
from qbit_seasonal_anime.core.discovery import RssSnapshot, flatten_rss_articles
from qbit_seasonal_anime.core.rules import create_or_update_rule, build_regex_pattern
from qbit_seasonal_anime.db.models import Episode, EpisodeNumberMapping, EpisodeStatus, Monitored, MonitoredStatus, RuleHistory, RuleOutcome, Settings, Feed, MatchHistory, utc_now

logger = logging.getLogger("qbit_seasonal_anime.core.confirmation")


LIVE_TORRENT_MIN_SCORE = 95.0


def _torrent_corresponds_to_article(torrent_name: str, article: Dict[str, Any]) -> bool:
    """
    Whether an existing qBittorrent torrent is the same release as a feed article.

    Torrent names get reordered or renamed after download (e.g. "X: Steel Ball Run - 02"
    becomes "X - Steel Ball Run - 02"), so compare structurally instead of by substring:
    same episode, compatible release group, and near-identical parsed titles.

    token_sort_ratio, not token_set_ratio: the latter scores a subset as 100, so
    "Sousou no Frieren Movie" would look identical to "Sousou no Frieren".
    """
    parsed = parse_release_title(torrent_name)
    if parsed.get("episode") is None or parsed.get("episode") != article.get("episode"):
        return False

    torrent_group = parsed.get("release_group")
    article_group = article.get("release_group")
    if torrent_group and article_group and torrent_group != article_group:
        return False

    normalised = normalize_title(parsed.get("title", ""))
    reference = normalize_title(article.get("title", ""))
    if not normalised or not reference:
        return False
    return float(fuzz.token_sort_ratio(normalised, reference)) >= LIVE_TORRENT_MIN_SCORE


def resolve_live_match_time(
    article: Optional[Dict[str, Any]],
    log_time: Optional[datetime],
    torrents: Optional[List[Tuple[str, datetime]]],
) -> Optional[datetime]:
    """
    When qBittorrent actually acted on a release, or None if it only ever sat in the cache.

    A cached article that the app's own fuzzy matcher likes proves nothing: qBittorrent
    may have had no rule yet, or its pattern never accepted that article. Only its own
    "accepted by rule" log line, or a torrent that exists for the release, is evidence.
    """
    if isinstance(log_time, datetime):
        return log_time
    if not article or not torrents:
        return None
    for name, added_on in torrents:
        if isinstance(added_on, datetime) and _torrent_corresponds_to_article(name, article):
            return added_on
    return None


def _episode_offset(session: Session, show: Monitored, feed_id: Optional[int]) -> Optional[int]:
    if show.id is None or feed_id is None:
        return None
    try:
        mapping = session.exec(
            select(EpisodeNumberMapping).where(
                EpisodeNumberMapping.monitored_id == show.id,
                EpisodeNumberMapping.feed_id == feed_id,
            )
        ).first()
    except Exception as e:
        logger.debug(f"Could not read episode mapping for '{show.display_name}': {e}")
        return None
    if mapping is None:
        return None
    try:
        return int(mapping.offset)
    except (TypeError, ValueError):
        return None


def _canonical_episode(
    session: Session,
    show: Monitored,
    feed_id: Optional[int],
    raw_episode: Optional[int],
    current_time: Optional[datetime] = None,
) -> Optional[int]:
    if raw_episode is None:
        return None
    offset = _episode_offset(session, show, feed_id)
    if offset is None:
        local_episode = raw_episode
    else:
        local_episode = raw_episode - offset

    if local_episode < 1:
        return None
    if show.total_episodes and local_episode > show.total_episodes:
        return None

    now = current_time or utc_now()
    if show.next_airing_episode and show.next_airing_at:
        air_at = show.next_airing_at
        if air_at.tzinfo is None:
            air_at = air_at.replace(tzinfo=timezone.utc)

        # An episode cannot air if its scheduled broadcast is in the future.
        # When an offset mapping is used, any mapping that produces an episode >= next_airing_episode
        # while next_airing_at is in the future is invalid evidence.
        if air_at > now + timedelta(hours=1):
            if offset is not None and local_episode >= show.next_airing_episode:
                logger.debug(
                    f"Episode {local_episode} for '{show.display_name}' rejected: "
                    f"Offset-mapped episode has not aired yet (next airing Ep {show.next_airing_episode} at {air_at})."
                )
                return None
            if local_episode > show.next_airing_episode:
                logger.debug(
                    f"Episode {local_episode} for '{show.display_name}' rejected: "
                    f"Episode is beyond next airing episode {show.next_airing_episode} which airs at {air_at}."
                )
                return None
            if show.total_episodes and local_episode >= show.total_episodes:
                logger.debug(
                    f"Finale episode {local_episode} for '{show.display_name}' rejected: "
                    f"Show finale airs in the future at {air_at}."
                )
                return None

    return local_episode


def _fetch_torrent_timestamps(qbit_client: QBitClient) -> List[Tuple[str, datetime]]:
    """(name, added_on) for every torrent, used as download evidence."""
    getter = getattr(qbit_client, "get_torrents", None)
    if not callable(getter):
        return []
    try:
        torrents = getter()
    except QbitClientError as e:
        logger.debug(f"Could not read qBittorrent torrents for match evidence: {e}")
        return []

    result: List[Tuple[str, datetime]] = []
    for torrent in torrents or []:
        name = getattr(torrent, "name", None)
        added_on = getattr(torrent, "added_on", None)
        if not name or added_on is None:
            continue
        try:
            result.append((str(name), datetime.fromtimestamp(float(added_on), tz=timezone.utc)))
        except (TypeError, ValueError, OSError):
            continue
    return result


def record_match_event(
    session: Session,
    monitored_id: Optional[int],
    show_name: str,
    rule_name: str,
    release_title: str,
    feed_name: Optional[str] = None,
    episode: Optional[int] = None,
    match_time: Optional[datetime] = None,
    matched_regex: Optional[str] = None,
) -> Optional[MatchHistory]:
    """Record a matched release in match_history if not already recorded."""
    try:
        if isinstance(match_time, datetime):
            if match_time.tzinfo:
                stored_time = match_time.astimezone(timezone.utc).replace(tzinfo=None)
            else:
                stored_time = match_time
        else:
            stored_time = utc_now().replace(tzinfo=None)

        existing = session.exec(
            select(MatchHistory).where(MatchHistory.release_title == release_title)
        ).first()
        if existing:
            updated_existing = False
            if matched_regex and not existing.matched_regex:
                existing.matched_regex = matched_regex
                updated_existing = True
            if stored_time and existing.created_at != stored_time:
                existing.created_at = stored_time
                updated_existing = True
            if updated_existing:
                session.add(existing)
            return existing

        record = MatchHistory(
            monitored_id=monitored_id,
            show_name=show_name,
            rule_name=rule_name,
            feed_name=feed_name,
            release_title=release_title,
            episode=episode,
            created_at=stored_time,
            matched_regex=matched_regex,
        )
        session.add(record)

        # Keep the latest 200 items in match_history
        all_hist = session.exec(select(MatchHistory).order_by(MatchHistory.created_at.desc())).all()
        if len(all_hist) > 200:
            for extra in all_hist[200:]:
                session.delete(extra)

        return record
    except Exception as e:
        logger.debug(f"Could not record match event: {e}")
        return None


ACCEPTANCE_RE = re.compile(r"RSS article '(?P<title>.+?)' is accepted by rule '(?P<rule>.+?)'\.")


def parse_acceptance_log_line(message: str) -> Optional[Tuple[str, str]]:
    """(release_title, rule_name) from a qBittorrent acceptance line, or None."""
    if not isinstance(message, str) or "is accepted by rule" not in message:
        return None
    match = ACCEPTANCE_RE.search(message)
    if not match:
        return None
    return match.group("title"), match.group("rule")


def ingest_log_acceptances(
    session: Session,
    entries: List[Tuple[int, str, datetime]],
    rule_patterns: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Record releases qBittorrent's log reports it accepted for our rules."""
    logs: List[str] = []

    shows_by_rule: Dict[str, Monitored] = {}
    for show in session.exec(select(Monitored)).all():
        if show.qbit_rule_name:
            shows_by_rule[show.qbit_rule_name] = show
    if not shows_by_rule:
        return logs

    feeds_by_id = {f.id: f.qbit_feed_name for f in session.exec(select(Feed)).all()}

    for _, message, when in entries:
        parsed = parse_acceptance_log_line(message)
        if parsed is None:
            continue
        release_title, rule_name = parsed
        show = shows_by_rule.get(rule_name)
        if show is None:
            continue

        source_episode = parse_release_title(release_title).get("episode")
        canonical = _canonical_episode(session, show, show.current_feed_id, source_episode)
        if canonical is None:
            logger.debug(
                f"Accepted release '{release_title}' for '{show.display_name}' has an episode number outside "
                f"the stored season mapping and was not recorded."
            )
            continue
        if canonical <= (show.last_confirmed_episode or 0):
            continue

        row = session.exec(
            select(Episode).where(
                Episode.monitored_id == show.id,
                Episode.episode_number == canonical,
            )
        ).first()
        if row:
            row.status = EpisodeStatus.COMPLETED
            row.source_episode = source_episode
            row.release_title = release_title
            row.downloaded_at = when
            session.add(row)

        show.last_confirmed_episode = canonical
        session.add(show)

        record_match_event(
            session=session,
            monitored_id=show.id,
            show_name=show.display_name,
            rule_name=rule_name,
            release_title=release_title,
            feed_name=feeds_by_id.get(show.current_feed_id),
            episode=canonical,
            match_time=when,
            matched_regex=(rule_patterns or {}).get(rule_name),
        )

        msg = (
            f"'{show.display_name}': qBittorrent accepted '{release_title}' "
            f"-> episode {canonical} recorded."
        )
        logger.info(msg)
        logs.append(msg)

    if logs:
        session.commit()
    return logs


def _best_article_match(
    articles: List[Dict[str, Any]],
    aliases: List[str],
    test_pattern: str,
    prepared_aliases: Optional[List[Tuple[str, str]]],
    parsed_articles: Optional[Dict[str, Dict[str, Any]]],
) -> Optional[Tuple[int, str, Dict[str, Any], Dict[str, Any]]]:
    """Return (episode, title, parsed, article) for the highest-episode matching article."""
    best: Optional[Tuple[int, str, Dict[str, Any], Dict[str, Any]]] = None
    for article in articles:
        title = article.get("title", "")
        is_match, _, parsed = match_release_to_show(
            title,
            aliases,
            test_pattern=test_pattern,
            prepared_aliases=prepared_aliases,
            parsed_cache=parsed_articles,
        )
        if not is_match:
            continue
        episode = parsed.get("episode")
        if episode is not None and (best is None or episode > best[0]):
            best = (episode, title, parsed, article)
    return best


FEED_SWITCH_GRACE_SECONDS = 300


def _find_release_on_feed(
    feed: Feed,
    articles_by_url: Dict[str, List[Dict[str, Any]]],
    aliases: List[str],
    test_pattern: str,
    prepared_aliases: Optional[List[Tuple[str, str]]],
    parsed_articles: Optional[Dict[str, Dict[str, Any]]],
) -> Optional[Tuple[int, str, Dict[str, Any], Dict[str, Any]]]:
    return _best_article_match(
        articles_by_url.get(feed.qbit_feed_url) or [],
        aliases,
        test_pattern,
        prepared_aliases,
        parsed_articles,
    )


def verify_and_confirm_rules_from_feeds(
    session: Session,
    qbit_client: QBitClient,
    settings: Settings,
    rss_snapshot: Optional[RssSnapshot] = None,
    parsed_articles: Optional[Dict[str, Dict[str, Any]]] = None,
    known_categories: Optional[Set[str]] = None,
) -> List[str]:
    """
    Verify and confirm rules against new and cached RSS feed articles.
    When a feed article matches an unconfirmed show's regex and aliases,
    it confirms the rule as working (Works) and updates the last confirmed episode.
    Unconfirmed shows that stay quiet on their own feed are re-checked against the
    other feeds by priority, so a release that only appears further down the list
    is picked up instead of waiting for the stall timer.
    """
    logs: List[str] = []

    stmt = select(Monitored).where(
        Monitored.current_feed_id.is_not(None),
        Monitored.status.in_([MonitoredStatus.UNCONFIRMED, MonitoredStatus.FIXED]),
    )
    monitored_shows = session.exec(stmt).all()
    if not monitored_shows:
        return logs

    try:
        if rss_snapshot is not None:
            articles_by_url = rss_snapshot.get()
        else:
            rss_tree = qbit_client.get_rss_items(with_data=True)
            articles_by_url = flatten_rss_articles(rss_tree)
    except QbitClientError as e:
        logger.warning(f"Could not fetch RSS articles for rule confirmation: {e}")
        return logs

    feeds_map = {f.id: f for f in session.exec(select(Feed)).all()}
    if parsed_articles is None:
        parsed_articles = {}

    ordered_feeds = sorted(feeds_map.values(), key=lambda f: f.priority)
    top_feed = ordered_feeds[0] if ordered_feeds else None
    failed_stmt = select(RuleHistory.monitored_id, RuleHistory.feed_id).where(
        RuleHistory.outcome.in_([RuleOutcome.STALLED, RuleOutcome.FALSE_POSITIVE, RuleOutcome.REPLACED]),
        RuleHistory.feed_id.isnot(None),
    )
    failed_feeds_by_show: Dict[int, List[int]] = {}
    for monitored_id, failed_feed_id in session.exec(failed_stmt).all():
        failed_feeds_by_show.setdefault(monitored_id, []).append(failed_feed_id)

    candidates = []
    for show in monitored_shows:
        feed = feeds_map.get(show.current_feed_id)
        if not feed:
            continue

        aliases = show.effective_aliases
        prepared_aliases = prepare_aliases(aliases)
        test_pattern = build_regex_pattern(aliases)
        found = _find_release_on_feed(feed, articles_by_url, aliases, test_pattern, prepared_aliases, parsed_articles)

        if found is not None and show.candidate_feed_id:
            show.candidate_feed_id = None
            show.candidate_feed_since = None
            session.add(show)

        if (
            found is None
            and show.status == MonitoredStatus.UNCONFIRMED
            and not show.feed_pinned
            and top_feed is not None
        ):
            # This show's feed never saw the release. Walk the other feeds by
            # priority, giving the preferred feed a short grace window so we do
            # not move just because the preferred feed is a little slower.
            detected_feed = None
            detected_title = None
            detected_group = None
            for other_feed in ordered_feeds:
                if other_feed.id in failed_feeds_by_show.get(show.id, []):
                    continue
                other_found = _find_release_on_feed(
                    other_feed, articles_by_url, aliases, test_pattern, prepared_aliases, parsed_articles
                )
                if other_found:
                    detected_feed = other_feed
                    detected_title = other_found[2].get("title")
                    detected_group = other_found[2].get("release_group")
                    break

            if detected_feed and detected_feed.id != feed.id:
                now = utc_now()
                since = show.candidate_feed_since
                if since and since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)
                is_recorded_candidate = show.candidate_feed_id == detected_feed.id and since is not None
                grace_elapsed = is_recorded_candidate and (now - since).total_seconds() >= FEED_SWITCH_GRACE_SECONDS

                if not is_recorded_candidate:
                    # First sighting: start the grace clock, but keep waiting.
                    show.candidate_feed_id = detected_feed.id
                    show.candidate_feed_since = now
                    session.add(show)
                    session.commit()
                    waiting = FEED_SWITCH_GRACE_SECONDS // 60
                    msg = (
                        f"'{show.display_name}': release seen on '{detected_feed.qbit_feed_name}' but not on "
                        f"'{top_feed.qbit_feed_name}' yet — waiting {waiting}m before moving the rule."
                    )
                    logger.info(msg)
                    logs.append(msg)
                elif grace_elapsed:
                    show.current_feed_id = detected_feed.id
                    show.matched_title = detected_title
                    show.matched_release_group = detected_group
                    show.candidate_feed_id = None
                    show.candidate_feed_since = None
                    try:
                        show.qbit_rule_name = create_or_update_rule(
                            qbit_client=qbit_client,
                            monitored=show,
                            feed=detected_feed,
                            base_dir=settings.base_dir,
                            category=settings.default_category,
                            ratio_limit=settings.default_seed_ratio,
                            release_group=detected_group,
                            known_categories=known_categories,
                        )
                    except QbitClientError as e:
                        logger.warning(f"Could not move rule for '{show.display_name}' to '{detected_feed.qbit_feed_name}': {e}")
                    session.add(RuleHistory(
                        monitored_id=show.id,
                        feed_id=detected_feed.id,
                        outcome=RuleOutcome.PENDING,
                        note=f"Release appeared on '{detected_feed.qbit_feed_name}' while '{feed.qbit_feed_name}' stayed silent",
                    ))
                    session.add(show)
                    session.commit()
                    msg = (
                        f"'{show.display_name}': release found on '{detected_feed.qbit_feed_name}', not on "
                        f"'{feed.qbit_feed_name}' — moved rule and kept it armed for the rest of the series."
                    )
                    logger.info(msg)
                    logs.append(msg)
                    feed = detected_feed
                    found = _find_release_on_feed(
                        feed, articles_by_url, aliases, test_pattern, prepared_aliases, parsed_articles
                    )

        if found is not None:
            best_ep, matched_title, best_parsed, best_art = found
            rule_name = show.qbit_rule_name or f"[Seasonal] {show.display_name}"
            candidates.append({
                "show": show,
                "feed": feed,
                "aliases": aliases,
                "rule_name": rule_name,
                "best_ep": best_ep,
                "matched_title": matched_title,
                "best_parsed": best_parsed,
                "best_art": best_art,
            })

    pending_events = []
    for candidate in candidates:
        show = candidate["show"]
        feed = candidate["feed"]
        aliases = candidate["aliases"]
        best_ep = candidate["best_ep"]
        matched_title = candidate["matched_title"]
        best_parsed = candidate["best_parsed"]
        rule_name = candidate["rule_name"]

        if best_parsed:
            if show.matched_title != best_parsed.get("title"):
                show.matched_title = best_parsed.get("title")
            if show.matched_release_group != best_parsed.get("release_group"):
                show.matched_release_group = best_parsed.get("release_group")

        if show.status == MonitoredStatus.UNCONFIRMED:
            show.status = MonitoredStatus.FIXED

            hist_stmt = (
                select(RuleHistory)
                .where(RuleHistory.monitored_id == show.id)
                .order_by(RuleHistory.created_at.desc())
                .limit(1)
            )
            latest_hist = session.exec(hist_stmt).first()
            if latest_hist and latest_hist.outcome == RuleOutcome.PENDING:
                latest_hist.outcome = RuleOutcome.CONFIRMED
                latest_hist.note = f"Verified with RSS release: {matched_title}"
                session.add(latest_hist)

            try:
                create_or_update_rule(
                    qbit_client=qbit_client,
                    monitored=show,
                    feed=feed,
                    base_dir=settings.base_dir,
                    category=settings.default_category,
                    ratio_limit=settings.default_seed_ratio,
                    release_group=show.matched_release_group,
                    known_categories=known_categories,
                )
            except Exception as e:
                logger.warning(f"Could not update cleaned rule in qBittorrent for '{show.display_name}': {e}")

            msg = f"Confirmed rule for '{show.display_name}' (Ep {best_ep}) via RSS '{matched_title}'. Cleaned up rule -> Works"
            logger.info(msg)
            logs.append(msg)

        regex_pat = show.custom_regex or build_regex_pattern(
            aliases,
            matched_title=show.matched_title,
            release_group=show.matched_release_group,
        )
        pending_events.append({
            "show_id": show.id,
            "show_name": show.display_name,
            "feed_name": feed.qbit_feed_name,
            "feed_id": feed.id,
            "rule_name": rule_name,
            "matched_title": matched_title,
            "best_art": candidate["best_art"],
            "best_parsed": candidate["best_parsed"],
            "episode": best_ep,
            "matched_regex": regex_pat,
        })

        session.add(show)

    match_pairs = [
        (event["rule_name"], event["matched_title"])
        for event in pending_events
        if event["matched_title"]
    ]

    log_acceptances: Dict[Tuple[str, str], Optional[datetime]] = {}
    if match_pairs:
        lookup = getattr(qbit_client, "find_log_acceptances", None)
        if callable(lookup):
            try:
                found = lookup(match_pairs)
                if isinstance(found, dict):
                    log_acceptances = found
            except Exception as e:
                logger.debug(f"Could not look up qBittorrent acceptances: {e}")

    needs_torrent_evidence = [
        pair for pair in match_pairs
        if not isinstance(log_acceptances.get(pair), datetime)
    ]
    torrents = _fetch_torrent_timestamps(qbit_client) if needs_torrent_evidence else []

    for event in pending_events:
        show = session.get(Monitored, event["show_id"])
        if show is None:
            continue

        rule_name = event["rule_name"]
        matched_title = event["matched_title"]
        live_at = resolve_live_match_time(
            event.get("best_parsed"),
            log_acceptances.get((rule_name, matched_title)),
            torrents,
        )

        if live_at is None:
            logger.debug(
                f"Cached release '{matched_title}' for '{event['show_name']}' matched locally but was never "
                f"accepted by qBittorrent — not recording it as a match."
            )
            continue

        canonical_episode = _canonical_episode(
            session,
            show,
            event.get("feed_id"),
            event["episode"],
        )
        if canonical_episode is None:
            logger.debug(
                f"Cached release '{matched_title}' for '{event['show_name']}' has an episode number outside "
                f"the stored season mapping and was not recorded."
            )
            continue

        current_episode = _canonical_episode(
            session,
            show,
            event.get("feed_id"),
            show.last_confirmed_episode,
        )
        if current_episode is None or canonical_episode > current_episode:
            show.last_confirmed_episode = canonical_episode
            session.add(show)

        canonical_row = session.exec(
            select(Episode).where(
                Episode.monitored_id == event["show_id"],
                Episode.episode_number == canonical_episode,
            )
        ).first()
        if canonical_row:
            canonical_row.status = EpisodeStatus.COMPLETED
            canonical_row.source_episode = event["episode"]
            canonical_row.release_title = matched_title
            canonical_row.downloaded_at = live_at
            session.add(canonical_row)
        else:
            canonical_row = Episode(
                monitored_id=event["show_id"],
                episode_number=canonical_episode,
                status=EpisodeStatus.COMPLETED,
                source_episode=event["episode"],
                release_title=matched_title,
                downloaded_at=live_at,
            )
            session.add(canonical_row)

        record_match_event(
            session=session,
            monitored_id=event["show_id"],
            show_name=event["show_name"],
            rule_name=rule_name,
            release_title=matched_title,
            feed_name=event["feed_name"],
            episode=canonical_episode,
            match_time=live_at,
            matched_regex=event["matched_regex"],
        )

    if pending_events:
        session.commit()

    return logs


# Backward compatibility alias
verify_and_confirm_torrents = verify_and_confirm_rules_from_feeds


def has_downloaded_final_episode(
    session: Session,
    show: Monitored,
    download_mode: str = DEFAULT_DOWNLOAD_MODE,
) -> bool:
    """Whether every episode of a finished season is accounted for.

    The two download engines have to answer this differently.

    ``rules`` mode lets qBittorrent own the downloads, so the only evidence the
    app has is a scalar plus the match log, and the historical checks stand.

    ``direct``/``observe`` mode makes this app the owner of an episode ledger,
    so the ledger is authoritative and *every* episode from 1 to
    ``total_episodes`` must be COMPLETED. A single wanted or failed episode
    therefore keeps the season open instead of letting a match log for the last
    episode retire it. An empty ledger falls through to the historical checks so
    that a show right after a mode switch still completes correctly.
    """
    if not show.total_episodes or show.total_episodes <= 0 or not show.id:
        return False

    # If AniList indicates an earlier episode is still to air, or finale has not aired yet, it cannot be completed
    now = utc_now()
    if show.next_airing_episode and show.total_episodes:
        if show.next_airing_episode < show.total_episodes:
            return False
        if show.next_airing_episode == show.total_episodes:
            air_at = show.next_airing_at
            if air_at and air_at.tzinfo is None:
                air_at = air_at.replace(tzinfo=timezone.utc)
            if air_at and air_at > now:
                return False

    episodes = session.exec(
        select(Episode).where(
            Episode.monitored_id == show.id,
            Episode.episode_number <= show.total_episodes,
        )
    ).all()
    if episodes:
        if (download_mode or DEFAULT_DOWNLOAD_MODE) in ("direct", "observe"):
            return all(episode.status == EpisodeStatus.COMPLETED for episode in episodes)
        if any(episode.status == EpisodeStatus.WANTED for episode in episodes):
            return False
        final_episode = next((ep for ep in episodes if ep.episode_number == show.total_episodes), None)
        if final_episode and final_episode.status == EpisodeStatus.COMPLETED:
            return True

    last_episode = show.last_confirmed_episode or 0
    if last_episode == show.total_episodes:
        return True

    # Legacy raw fallback: handle un-migrated raw last_confirmed_episode
    offset = _episode_offset(session, show, show.current_feed_id)
    if offset is not None and last_episode > show.total_episodes:
        if (last_episode - offset) == show.total_episodes:
            show.last_confirmed_episode = last_episode - offset
            session.add(show)
            session.commit()
            return True

    match_conditions = [MatchHistory.monitored_id == show.id]
    if show.display_name:
        match_conditions.append(MatchHistory.show_name == show.display_name)

    history_rows = session.exec(
        select(MatchHistory).where(or_(*match_conditions))
    ).all()
    for history in history_rows:
        history_episode = history.episode
        if history_episode is None:
            continue
        if history_episode == show.total_episodes:
            if last_episode < history_episode:
                show.last_confirmed_episode = history_episode
                session.add(show)
            return True

    return False