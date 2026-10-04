import asyncio
import logging
from datetime import timezone, timedelta
from typing import Any, Dict, List, Optional, Set
from sqlmodel import Session, select
from qbit_seasonal_anime.clients.anilist import AniListClient, AniListError, get_current_and_next_season
from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError, QbitConnectionError
from qbit_seasonal_anime.core.confirmation import verify_and_confirm_torrents, has_downloaded_final_episode
from qbit_seasonal_anime.core.discovery import RssSnapshot, discover_feed_for_show, flatten_rss_articles
from qbit_seasonal_anime.core.rules import build_rule_definition, build_rule_name, create_or_update_rule, delete_rule, disable_rule
from qbit_seasonal_anime.core.stall import check_and_handle_stalls
from qbit_seasonal_anime.db.models import (
    Episode,
    EpisodeNumberMapping,
    EpisodeStatus,
    Feed,
    MatchHistory,
    Monitored,
    MonitoredStatus,
    RuleHistory,
    RuleOutcome,
    Settings,
    utc_now,
)

logger = logging.getLogger("qbit_seasonal_anime.core.supervisor")


def _format_age(seconds: float) -> str:
    """Render a duration as '45s', '12m' or '3h 20m' for log lines."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h" if not minutes else f"{hours}h {minutes}m"


def _rules_are_equivalent(current: Dict[str, Any], desired: Dict[str, Any]) -> bool:
    """Return True if an existing qBittorrent rule matches the desired definition.

    Deliberately ignores qBittorrent-owned state (lastMatch, previouslyMatchedEpisodes):
    those change on every match, and comparing them would rewrite rules continuously,
    which is exactly what would wipe that state.
    """
    try:
        return (
            current.get("mustContain") == desired.get("mustContain")
            and current.get("mustNotContain") == desired.get("mustNotContain")
            and current.get("affectedFeeds") == desired.get("affectedFeeds")
            and current.get("savePath") == desired.get("savePath")
            and (current.get("assignedCategory") or "") == (desired.get("assignedCategory") or "")
            and current.get("useRegex") == desired.get("useRegex")
            and current.get("enabled") == desired.get("enabled")
        )
    except Exception:
        return False


class Supervisor:
    def __init__(self, session: Session, qbit: QBitClient, anilist: AniListClient, settings: Settings):
        self.session = session
        self.qbit = qbit
        self.anilist = anilist
        self.settings = settings
        self._known_categories: Set[str] = set()

    def sync_feeds(self) -> List[str]:
        """Fetch RSS feeds from qBittorrent and ensure they are registered in the database, pruning any removed feeds."""
        logs = []
        had_pending_changes = bool(self.session.new or self.session.dirty or self.session.deleted)
        try:
            qbit_feeds = self.qbit.get_rss_feeds_flat()
        except QbitClientError as e:
            logger.warning(f"Could not sync RSS feeds from qBittorrent: {e}")
            return [f"Feed sync error: {e}"]

        db_feeds = self.session.exec(select(Feed).order_by(Feed.priority)).all()
        existing_feeds_by_url = {f.qbit_feed_url: f for f in db_feeds}
        qbit_urls = {qf["url"]: qf["name"] for qf in qbit_feeds}

        added = 0
        removed = 0
        renamed = 0
        priorities_changed = False

        for url, f in list(existing_feeds_by_url.items()):
            if url not in qbit_urls:
                shows_on_feed = self.session.exec(
                    select(Monitored).where(Monitored.current_feed_id == f.id)
                ).all()
                for show in shows_on_feed:
                    show.current_feed_id = None
                    if show.status == MonitoredStatus.FIXED:
                        show.status = MonitoredStatus.UNCONFIRMED
                    self.session.add(show)

                hist_on_feed = self.session.exec(
                    select(RuleHistory).where(RuleHistory.feed_id == f.id)
                ).all()
                for h in hist_on_feed:
                    h.feed_id = None
                    self.session.add(h)

                self.session.delete(f)
                del existing_feeds_by_url[url]
                removed += 1
                logs.append(f"Removed deleted qBit RSS feed: '{f.qbit_feed_name}'")

        next_priority = len(existing_feeds_by_url) + 1
        for qf in qbit_feeds:
            url = qf["url"]
            name = qf["name"]
            if url not in existing_feeds_by_url:
                new_feed = Feed(qbit_feed_name=name, qbit_feed_url=url, priority=next_priority)
                self.session.add(new_feed)
                existing_feeds_by_url[url] = new_feed
                next_priority += 1
                added += 1
                logs.append(f"Discovered new qBit RSS feed: '{name}' (Priority {new_feed.priority})")
            else:
                existing_feed = existing_feeds_by_url[url]
                if existing_feed.qbit_feed_name != name:
                    existing_feed.qbit_feed_name = name
                    self.session.add(existing_feed)
                    renamed += 1

        self.session.flush()

        remaining_feeds = list(existing_feeds_by_url.values())
        remaining_feeds.sort(key=lambda x: x.priority)
        for idx, feed_item in enumerate(remaining_feeds, start=1):
            if feed_item.priority != idx:
                feed_item.priority = idx
                self.session.add(feed_item)
                priorities_changed = True

        if had_pending_changes or added or removed or renamed or priorities_changed:
            self.session.commit()

        total = len(existing_feeds_by_url)
        summary_parts = []
        if added > 0:
            summary_parts.append(f"added {added}")
        if removed > 0:
            summary_parts.append(f"removed {removed}")
        if renamed > 0:
            summary_parts.append(f"updated {renamed}")

        if summary_parts:
            logs.append(f"Synchronized RSS feeds: {', '.join(summary_parts)} (Total: {total}).")
        else:
            logs.append(f"Verified {total} RSS feeds configured in qBittorrent.")

        return logs

    def bootstrap_unassigned_shows(
        self,
        rss_snapshot: Optional[RssSnapshot] = None,
        parsed_articles: Optional[Dict[str, Dict[str, Any]]] = None,
        known_categories: Optional[Set[str]] = None,
    ) -> List[str]:
        """Discover feeds and create initial rules for unassigned or newly added shows using the proactive approach."""
        logs = []
        stmt = select(Monitored).where(
            (Monitored.current_feed_id.is_(None)) | (Monitored.qbit_rule_name.is_(None)),
            Monitored.status.in_([MonitoredStatus.UNCONFIRMED, MonitoredStatus.STALLED]),
        )
        unassigned_shows = self.session.exec(stmt).all()
        all_feeds = self.session.exec(select(Feed).order_by(Feed.priority)).all()

        if not unassigned_shows or not all_feeds:
            return logs

        top_feed = all_feeds[0]
        if parsed_articles is None:
            parsed_articles = {}

        try:
            if rss_snapshot is not None:
                cached_articles = rss_snapshot.get()
            else:
                rss_tree = self.qbit.get_rss_items(with_data=True)
                cached_articles = flatten_rss_articles(rss_tree)
        except QbitClientError as e:
            logger.warning(f"Could not fetch RSS cache for bootstrap: {e}")
            cached_articles = {}

        failed_stmt = select(RuleHistory.monitored_id, RuleHistory.feed_id).where(
            RuleHistory.outcome.in_([RuleOutcome.STALLED, RuleOutcome.FALSE_POSITIVE, RuleOutcome.REPLACED]),
            RuleHistory.feed_id.isnot(None),
        )
        excluded_by_show: Dict[int, List[int]] = {}
        for monitored_id, feed_id in self.session.exec(failed_stmt).all():
            excluded_by_show.setdefault(monitored_id, []).append(feed_id)

        for show in unassigned_shows:
            excluded = excluded_by_show.get(show.id, [])

            res = discover_feed_for_show(
                monitored=show,
                feeds=all_feeds,
                qbit_client=self.qbit,
                excluded_feed_ids=excluded,
                cached_articles_by_url=cached_articles,
                rss_snapshot=rss_snapshot,
                parsed_articles=parsed_articles,
            )
            if res:
                chosen_feed, obs_group, matched_title = res
                show.matched_title = matched_title
                show.matched_release_group = obs_group
                try:
                    rule_name = create_or_update_rule(
                        qbit_client=self.qbit,
                        monitored=show,
                        feed=chosen_feed,
                        base_dir=self.settings.base_dir,
                        category=self.settings.default_category,
                        ratio_limit=self.settings.default_seed_ratio,
                        release_group=obs_group,
                        title_language=getattr(self.settings, "title_language", "english"),
                        known_categories=known_categories,
                    )
                    show.current_feed_id = chosen_feed.id
                    show.qbit_rule_name = rule_name
                    show.status = MonitoredStatus.FIXED
                    self.session.add(show)

                    hist = RuleHistory(
                        monitored_id=show.id,
                        feed_id=chosen_feed.id,
                        created_at=utc_now(),
                        outcome=RuleOutcome.CONFIRMED,
                        note=f"Verified rule created on '{chosen_feed.qbit_feed_name}'",
                    )
                    self.session.add(hist)
                    self.session.commit()

                    msg = f"Created verified rule for '{show.display_name}' on feed '{chosen_feed.qbit_feed_name}' (Works)"
                    logger.info(msg)
                    logs.append(msg)
                except QbitClientError as e:
                    err_msg = f"Failed creating rule for '{show.display_name}': {e}"
                    logger.error(err_msg)
            else:
                target_feed = top_feed
                if top_feed.id in excluded:
                    avail = [f for f in all_feeds if f.id not in excluded]
                    target_feed = avail[0] if avail else None

                if target_feed:
                    try:
                        rule_name = create_or_update_rule(
                            qbit_client=self.qbit,
                            monitored=show,
                            feed=target_feed,
                            base_dir=self.settings.base_dir,
                            category=self.settings.default_category,
                            ratio_limit=self.settings.default_seed_ratio,
                            release_group=None,
                            title_language=getattr(self.settings, "title_language", "english"),
                            known_categories=known_categories,
                        )
                        show.current_feed_id = target_feed.id
                        show.qbit_rule_name = rule_name
                        show.status = MonitoredStatus.UNCONFIRMED
                        self.session.add(show)

                        hist = RuleHistory(
                            monitored_id=show.id,
                            feed_id=target_feed.id,
                            created_at=utc_now(),
                            outcome=RuleOutcome.PENDING,
                            note=f"Proactive rule armed on #{target_feed.priority} feed '{target_feed.qbit_feed_name}'",
                        )
                        self.session.add(hist)
                        self.session.commit()

                        msg = f"Armed proactive rule for '{show.display_name}' on Priority #{target_feed.priority} feed '{target_feed.qbit_feed_name}' (Testing)"
                        logger.info(msg)
                        logs.append(msg)
                    except QbitClientError as e:
                        err_msg = f"Failed arming proactive rule for '{show.display_name}': {e}"
                        logger.error(err_msg)
                        logs.append(f"Warning: {err_msg}")

        return logs

    async def sync_anilist_schedule(self, *, force: bool = False, hunting: bool = False) -> List[str]:
        """Fetch updated episode numbers, air dates, and status from AniList. Also imports newly added shows.

        Skips the query until the cached schedule is older than the routine refresh
        interval, unless force is set.
        """
        logs = []
        if not self.settings.anilist_username.strip():
            return logs

        min_age_seconds = max(0, self.settings.refresh_interval_minutes) * 60
        if not force and not self.anilist.is_sync_due(min_age_seconds):
            age_seconds = self.anilist.seconds_since_last_sync() or 0.0
            remaining = min_age_seconds - age_seconds
            reason = "hunting" if hunting else "routine"
            return [
                f"AniList schedule sync deferred ({reason} pass): last synced {_format_age(age_seconds)} ago, "
                f"next due in {_format_age(remaining)} (refresh interval {self.settings.refresh_interval_minutes}m)."
            ]

        existing_shows = {s.anilist_id: s for s in self.session.exec(select(Monitored)).all()}
        try:
            seasonal_list = await self.anilist.fetch_user_seasonal_anime(
                self.settings.anilist_username,
                monitored_anilist_ids=set(existing_shows.keys()),
            )
        except AniListError as e:
            logger.warning(f"Could not refresh AniList schedule: {e}")
            return [f"AniList schedule sync error: {e}"]

        new_shows_count = 0
        pref_lang = getattr(self.settings, "title_language", "english")

        for data in seasonal_list:
            aid = data.get("anilist_id") or data.get("id")
            if not aid:
                continue
            
            en_t = data.get("title_english")
            ro_t = data.get("title_romaji")
            chosen_name = en_t if (pref_lang == "english" and en_t) else (ro_t or data["display_name"])

            if aid in existing_shows:
                show = existing_shows[aid]
                updated = False

                if data.get("title_romaji") and show.title_romaji != data.get("title_romaji"):
                    show.title_romaji = data.get("title_romaji")
                    updated = True
                if data.get("title_english") and show.title_english != data.get("title_english"):
                    show.title_english = data.get("title_english")
                    updated = True
                if show.display_name != chosen_name:
                    show.display_name = chosen_name
                    updated = True
                if data.get("total_episodes") != show.total_episodes:
                    show.total_episodes = data.get("total_episodes")
                    updated = True
                if data.get("cover_image") and show.cover_image != data.get("cover_image"):
                    show.cover_image = data.get("cover_image")
                    updated = True
                if data.get("season") and show.season_name != data.get("season"):
                    show.season_name = data.get("season")
                    updated = True
                if data.get("season_year") and show.season_year != data.get("season_year"):
                    show.season_year = data.get("season_year")
                    updated = True

                current_aliases = show.aliases
                incoming_aliases = {
                    alias.strip()
                    for alias in data.get("aliases", [])
                    if alias and alias.strip()
                }
                if not incoming_aliases.issubset(set(current_aliases)):
                    show.aliases = list(set(current_aliases) | incoming_aliases)
                    updated = True

                # The user marking the show COMPLETED on their AniList list is a
                # completion signal in its own right: it means they are done with the
                # series and want the rule stood down, whether or not we ever
                # confirmed every episode.
                anilist_marked_complete = (data.get("list_status") == "COMPLETED")
                is_finished = (data.get("status") == "FINISHED")
                if anilist_marked_complete or (is_finished and data.get("next_airing_episode") is None):
                    if anilist_marked_complete or has_downloaded_final_episode(self.session, show):
                        if show.next_airing_episode is not None:
                            show.next_airing_episode = None
                            updated = True
                        if show.next_airing_at is not None:
                            show.next_airing_at = None
                            updated = True
                        if show.status != MonitoredStatus.COMPLETED:
                            show.status = MonitoredStatus.COMPLETED
                            if show.qbit_rule_name:
                                disable_rule(self.qbit, show.qbit_rule_name)
                            updated = True
                            if anilist_marked_complete:
                                msg = f"Show '{show.display_name}' marked COMPLETED on AniList. Status -> COMPLETED, rule disabled."
                            else:
                                msg = f"Show '{show.display_name}' completed all {show.total_episodes} episodes. Status -> COMPLETED, rule disabled."
                            logger.info(msg)
                            logs.append(msg)
                    else:
                        final_ep = show.total_episodes or show.next_airing_episode
                        if final_ep and show.next_airing_episode != final_ep:
                            show.next_airing_episode = final_ep
                            updated = True
                else:
                    if data.get("next_airing_episode") != show.next_airing_episode:
                        show.next_airing_episode = data.get("next_airing_episode")
                        updated = True
                    if data.get("next_airing_at") != show.next_airing_at:
                        show.next_airing_at = data.get("next_airing_at")
                        updated = True

                # Re-open a show we completed on download evidence alone, in case that
                # evidence was wrong. A completion the user asked for on AniList is
                # deliberate, so it stands; flipping the list back to CURRENT brings the
                # show back through here and re-opens it.
                if (
                    show.status == MonitoredStatus.COMPLETED
                    and not anilist_marked_complete
                    and not has_downloaded_final_episode(self.session, show)
                ):
                    show.status = (
                        MonitoredStatus.FIXED
                        if (show.last_confirmed_episode or 0) > 0
                        else MonitoredStatus.UNCONFIRMED
                    )
                    updated = True

                if updated:
                    self.session.add(show)
            else:
                from qbit_seasonal_anime.core.rules import sanitize_folder_name
                new_show = Monitored(
                    anilist_id=aid,
                    display_name=chosen_name,
                    title_romaji=data.get("title_romaji"),
                    title_english=data.get("title_english"),
                    aliases_json="[]",
                    status=MonitoredStatus.UNCONFIRMED,
                    total_episodes=data.get("total_episodes"),
                    next_airing_episode=data.get("next_airing_episode"),
                    next_airing_at=data.get("next_airing_at"),
                    save_folder=sanitize_folder_name(chosen_name),
                    cover_image=data.get("cover_image"),
                    season_name=data.get("season"),
                    season_year=data.get("season_year"),
                )
                new_show.aliases = data.get("aliases", [])
                self.session.add(new_show)
                existing_shows[aid] = new_show
                new_shows_count += 1

        self.session.commit()
        msg = f"Synced AniList schedule for {len(seasonal_list)} seasonal shows."
        if new_shows_count > 0:
            msg += f" (Discovered and added {new_shows_count} new seasonal shows to Monitored)"
        logs.append(msg)
        return logs

    def sync_active_rules(self) -> List[str]:
        """Ensure active rules in qBittorrent exist and have up-to-date definitions without redundant API calls."""
        logs = []
        had_pending_changes = bool(self.session.new or self.session.dirty or self.session.deleted)
        active_shows = self.session.exec(
            select(Monitored).where(
                Monitored.current_feed_id.is_not(None),
                Monitored.status.in_([MonitoredStatus.UNCONFIRMED, MonitoredStatus.FIXED]),
            )
        ).all()
        feeds_map = {f.id: f for f in self.session.exec(select(Feed)).all()}

        try:
            client = self.qbit.get_client()
            existing_rules = client.rss_rules()
        except Exception as e:
            logger.debug(f"Could not fetch existing RSS rules from qBittorrent: {e}")
            existing_rules = {}

        refreshed = 0
        try:
            for show in active_shows:
                feed = feeds_map.get(show.current_feed_id)
                if not feed:
                    continue

                rule_name = show.qbit_rule_name or build_rule_name(show.id or 0, show.display_name)
                current_def = existing_rules.get(rule_name)
                desired_def = build_rule_definition(
                    monitored=show,
                    feed_url=feed.qbit_feed_url,
                    base_dir=self.settings.base_dir,
                    category=self.settings.default_category,
                    ratio_limit=self.settings.default_seed_ratio,
                    release_group=show.matched_release_group,
                    title_language=getattr(self.settings, "title_language", "english"),
                    previous_rule=current_def,
                )

                if current_def and _rules_are_equivalent(current_def, desired_def):
                    continue

                try:
                    self.qbit.set_rss_rule(rule_name=rule_name, rule_def=desired_def)
                    show.qbit_rule_name = rule_name
                    self.session.add(show)
                    refreshed += 1
                except QbitConnectionError:
                    # Every remaining show would fail identically against a dead
                    # socket, so stop and let the caller re-enter its reconnect
                    # backoff instead of logging one warning per show.
                    logger.warning(f"qBittorrent connection lost while refreshing '{show.display_name}'; deferring remaining shows")
                    raise
                except QbitClientError as e:
                    logger.warning(f"Could not refresh rule for '{show.display_name}': {e}")
                    logs.append(f"Warning: Failed updating rule for '{show.display_name}': {e}")
        finally:
            # Commit even when bailing out, so rules already written to
            # qBittorrent and any earlier pending changes are not lost.
            if had_pending_changes or refreshed > 0:
                self.session.commit()

        if refreshed > 0:
            logs.append(f"Synchronized {refreshed} updated rules in qBittorrent.")
        return logs

    def reconcile_schedule_rollover(self) -> List[str]:
        """
        Auto-roll episode schedules (+7 days weekly heuristic) when AniList API is unreachable,
        rate-limited, or lagging behind TV broadcast.
        """
        logs = []
        now = utc_now()
        active_stmt = select(Monitored).where(
            Monitored.status.in_([MonitoredStatus.UNCONFIRMED, MonitoredStatus.FIXED, MonitoredStatus.STALLED])
        )
        shows = self.session.exec(active_stmt).all()

        for show in shows:
            air_at = show.next_airing_at
            if not air_at:
                continue
            if air_at.tzinfo is None:
                air_at = air_at.replace(tzinfo=timezone.utc)

            if air_at > now:
                continue

            current_ep = show.next_airing_episode or 1

            if show.total_episodes and current_ep >= show.total_episodes:
                if has_downloaded_final_episode(self.session, show):
                    show.next_airing_episode = None
                    show.next_airing_at = None
                    show.status = MonitoredStatus.COMPLETED
                    if show.qbit_rule_name:
                        disable_rule(self.qbit, show.qbit_rule_name)
                    self.session.add(show)
                    msg = f"Show '{show.display_name}' completed all {show.total_episodes} episodes. Status -> COMPLETED, rule disabled."
                    logger.info(msg)
                    logs.append(msg)
                continue

            has_confirmed_release = bool(show.last_confirmed_episode and show.last_confirmed_episode > 0)
            is_overdue_24h = air_at <= (now - timedelta(hours=24))

            if has_confirmed_release or is_overdue_24h:
                new_ep = current_ep + 1
                new_air = air_at + timedelta(days=7)
                while new_air <= now:
                    new_air += timedelta(days=7)
                    new_ep += 1

                if show.total_episodes and new_ep >= show.total_episodes:
                    new_ep = show.total_episodes

                show.next_airing_episode = new_ep
                show.next_airing_at = new_air
                self.session.add(show)

                air_str = new_air.strftime("%d/%m %H:%M UTC")
                msg = f"Show '{show.display_name}': Rolled schedule to Ep {new_ep} ({air_str})."
                logger.info(msg)
                logs.append(msg)

        if logs:
            self.session.commit()

        return logs

    def prune_past_season_shows(self) -> List[str]:
        """
        Remove completed shows from past seasons when the next season begins.
        Continuing cours that extend into the current or future season are preserved.
        """
        logs = []
        now = utc_now()
        ((cur_season, cur_year), _) = get_current_and_next_season()
        season_order = {"WINTER": 1, "SPRING": 2, "SUMMER": 3, "FALL": 4}
        cur_season_idx = cur_year * 10 + season_order.get(cur_season.upper(), 0)

        all_shows = self.session.exec(select(Monitored)).all()

        for show in all_shows:
            if not show.season_year or not show.season_name:
                continue

            show_season_idx = show.season_year * 10 + season_order.get(show.season_name.upper(), 0)
            if show_season_idx >= cur_season_idx:
                continue

            # Non-completed shows from past seasons are never pruned
            if show.status != MonitoredStatus.COMPLETED:
                continue

            # Grace period check: if next_airing_at was recent, the release may still be pending
            air_at = show.next_airing_at
            if air_at and air_at.tzinfo is None:
                air_at = air_at.replace(tzinfo=timezone.utc)
            if air_at and air_at > (now - timedelta(days=7)):
                continue

            # Check episode ledger: if any episode is not COMPLETED, do not prune
            episodes = self.session.exec(
                select(Episode).where(Episode.monitored_id == show.id)
            ).all()
            if any(ep.status != EpisodeStatus.COMPLETED for ep in episodes):
                continue

            if show.qbit_rule_name:
                try:
                    delete_rule(self.qbit, show.qbit_rule_name)
                except Exception as e:
                    logger.debug(f"Could not delete rule '{show.qbit_rule_name}' during seasonal prune: {e}")
                show.qbit_rule_name = None

            for h in self.session.exec(select(RuleHistory).where(RuleHistory.monitored_id == show.id)).all():
                self.session.delete(h)
            for m in self.session.exec(select(MatchHistory).where(MatchHistory.monitored_id == show.id)).all():
                self.session.delete(m)
            for mapping in self.session.exec(select(EpisodeNumberMapping).where(EpisodeNumberMapping.monitored_id == show.id)).all():
                self.session.delete(mapping)
            for ep in episodes:
                self.session.delete(ep)

            self.session.delete(show)
            msg = f"Season transition ({cur_season} {cur_year}): Pruned completed show '{show.display_name}' from past season ({show.season_name} {show.season_year})."
            logger.info(msg)
            logs.append(msg)

        if logs:
            self.session.commit()

        return logs

    async def run_full_cycle(self, *, hunting: bool = False) -> List[str]:
        """Execute one complete supervision iteration.

        hunting marks a short release-polling pass for the AniList sync log line.
        """
        all_logs: List[str] = []
        rss_snapshot = RssSnapshot(self.qbit)
        parsed_articles: Dict[str, Dict[str, Any]] = {}
        self._known_categories.clear()

        all_logs.extend(await asyncio.to_thread(self.sync_feeds))

        all_logs.extend(await self.sync_anilist_schedule(hunting=hunting))

        all_logs.extend(await asyncio.to_thread(
            self.bootstrap_unassigned_shows,
            rss_snapshot,
            parsed_articles,
            self._known_categories,
        ))

        all_logs.extend(await asyncio.to_thread(
            verify_and_confirm_torrents,
            self.session,
            self.qbit,
            self.settings,
            rss_snapshot,
            parsed_articles,
            self._known_categories,
        ))

        all_logs.extend(await asyncio.to_thread(self.reconcile_schedule_rollover))

        all_logs.extend(await asyncio.to_thread(self.sync_active_rules))

        all_logs.extend(await asyncio.to_thread(
            check_and_handle_stalls,
            self.session,
            self.qbit,
            self.settings,
            rss_snapshot,
            parsed_articles,
            self._known_categories,
        ))

        all_logs.extend(await asyncio.to_thread(self.prune_past_season_shows))

        total_shows = self.session.exec(select(Monitored)).all()
        now = utc_now()
        works_cnt = sum(1 for s in total_shows if s.status == MonitoredStatus.FIXED)
        stalled_cnt = sum(1 for s in total_shows if s.status == MonitoredStatus.STALLED)
        paused_cnt = sum(1 for s in total_shows if s.status == MonitoredStatus.PAUSED)
        completed_cnt = sum(1 for s in total_shows if s.status == MonitoredStatus.COMPLETED)
        upcoming_cnt = 0
        testing_cnt = 0
        for s in total_shows:
            if s.status == MonitoredStatus.UNCONFIRMED:
                air_at = s.next_airing_at
                if air_at and air_at.tzinfo is None:
                    air_at = air_at.replace(tzinfo=timezone.utc)
                is_unreleased = (
                    (s.next_airing_episode == 1 or s.next_airing_episode is None)
                    and (s.last_confirmed_episode or 0) == 0
                    and (air_at is None or air_at > now)
                )
                if is_unreleased:
                    upcoming_cnt += 1
                else:
                    testing_cnt += 1

        summary = f"Summary: {works_cnt} Works | {upcoming_cnt} Upcoming"
        if testing_cnt > 0:
            summary += f" | {testing_cnt} Testing"
        if stalled_cnt > 0:
            summary += f" | {stalled_cnt} Stalled"
        if completed_cnt > 0:
            summary += f" | {completed_cnt} Completed"
        if paused_cnt > 0:
            summary += f" | {paused_cnt} Paused"
        all_logs.append(summary)

        return all_logs
