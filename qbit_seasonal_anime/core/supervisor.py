import asyncio
import inspect
import logging
from datetime import timezone, timedelta
from typing import Any, Dict, List, Optional, Set
from uuid import uuid4
from sqlalchemy import func
from sqlmodel import Session, select
from qbit_seasonal_anime.clients.anilist import AniListClient, AniListError, AniListRateLimited, get_current_and_next_season
from qbit_seasonal_anime.clients.qbit import QBitClient, QbitClientError, QbitConnectionError
from qbit_seasonal_anime.config import DEFAULT_DOWNLOAD_MODE
from qbit_seasonal_anime.core.confirmation import verify_and_confirm_torrents, has_downloaded_final_episode
from qbit_seasonal_anime.core.discovery import RssSnapshot, discover_feed_for_show, flatten_rss_articles
from qbit_seasonal_anime.core.grabber import evaluate_and_grab_releases, sync_show_episodes, update_episode_status
from qbit_seasonal_anime.core.matching import match_release_to_show, parse_release_title, prepare_aliases
from qbit_seasonal_anime.core.rules import build_rule_definition, build_rule_name, create_or_update_rule, delete_rule, disable_rule
from qbit_seasonal_anime.core.stall import check_and_handle_stalls
from qbit_seasonal_anime.db.models import Episode, EpisodeNumberMapping, EpisodeScheduleState, EpisodeStatus, Feed, MatchHistory, Monitored, MonitoredStatus, RuleHistory, RuleOutcome, SeenFeedItem, Settings, utc_now
from qbit_seasonal_anime.db.session import acquire_supervision_lease, heartbeat_supervision_lease, release_supervision_lease

logger = logging.getLogger("qbit_seasonal_anime.core.supervisor")

# How stale a show's per-episode air dates may get before they are refetched.
EPISODE_SCHEDULE_MAX_AGE_SECONDS = 6 * 3600


def _aware(value):
    """SQLite hands back naive datetimes; anything compared to utc_now() must be aware."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
# How long another feed has to keep carrying a release before a show moves to it.
FEED_SWITCH_GRACE_SECONDS = 300


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
        self.anilist_sync_succeeded: Optional[bool] = None

    def download_mode(self) -> str:
        """The configured download engine, normalised to a known value.

        Anything unrecognised falls back to ``rules`` so a hand-edited or
        corrupt setting can never silently disable downloads.
        """
        mode = (getattr(self.settings, "download_mode", DEFAULT_DOWNLOAD_MODE) or DEFAULT_DOWNLOAD_MODE)
        mode = str(mode).strip().lower()
        return mode if mode in {"rules", "observe", "direct"} else DEFAULT_DOWNLOAD_MODE

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
                    show.feed_pinned = False
                    show.candidate_feed_id = None
                    show.candidate_feed_since = None
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
        create_qbit_rules: bool = True,
        mark_fixed: bool = True,
    ) -> List[str]:
        """Discover feeds and assign shows to them, optionally creating RSS rules.

        The direct engine never owns a qBittorrent rule, so it passes
        ``create_qbit_rules=False`` and only the feed assignment is recorded.
        Without a rule name, though, every show would look unassigned forever,
        so the assignment filter is narrowed to shows that have no feed at all.
        """
        logs = []
        if create_qbit_rules:
            assignment_filter = (
                (Monitored.current_feed_id.is_(None))
                | (Monitored.qbit_rule_name.is_(None))
            )
        else:
            assignment_filter = Monitored.current_feed_id.is_(None)
        stmt = select(Monitored).where(
            assignment_filter,
            Monitored.status.in_([MonitoredStatus.UNCONFIRMED, MonitoredStatus.STALLED]),
        )
        unassigned_shows = self.session.exec(stmt).all()
        all_feeds = self.session.exec(select(Feed).order_by(Feed.priority)).all()

        if not unassigned_shows or not all_feeds:
            return logs

        if not create_qbit_rules:
            logs.extend(self._reassign_direct_feeds(all_feeds))

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
                if not create_qbit_rules:
                    show.current_feed_id = chosen_feed.id
                    if mark_fixed:
                        show.status = MonitoredStatus.FIXED
                    self.session.add(show)
                    self.session.add(RuleHistory(
                        monitored_id=show.id,
                        feed_id=chosen_feed.id,
                        created_at=utc_now(),
                        outcome=RuleOutcome.CONFIRMED,
                        note=f"Auto-detected on '{chosen_feed.qbit_feed_name}' (direct mode)",
                    ))
                    self.session.commit()
                    msg = f"Assigned '{show.display_name}' to feed '{chosen_feed.qbit_feed_name}' (direct mode)"
                    logger.info(msg)
                    logs.append(msg)
                    continue
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
                    if not create_qbit_rules:
                        show.current_feed_id = target_feed.id
                        if mark_fixed:
                            show.status = MonitoredStatus.FIXED
                        self.session.add(show)
                        self.session.add(RuleHistory(
                            monitored_id=show.id,
                            feed_id=target_feed.id,
                            created_at=utc_now(),
                            outcome=RuleOutcome.PENDING,
                            note=f"Assigned to #{target_feed.priority} feed '{target_feed.qbit_feed_name}' (direct mode)",
                        ))
                        self.session.commit()
                        msg = f"Assigned '{show.display_name}' to Priority #{target_feed.priority} feed '{target_feed.qbit_feed_name}' (direct mode)"
                        logger.info(msg)
                        logs.append(msg)
                        continue
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

    async def sync_anilist_schedule(
        self,
        *,
        force: bool = False,
        hunting: bool = False,
        direct_mode: bool = False,
    ) -> List[str]:
        """Fetch updated episode numbers, air dates, and status from AniList. Also imports newly added shows.

        Skips the query until the cached schedule is older than the routine refresh
        interval, unless force is set.

        ``direct_mode`` additionally refreshes the per-episode airing schedule on
        its own, slower cadence, because the direct engine needs an authoritative
        ``air_at`` per episode before it will backfill a stale release.
        """
        logs = []
        if not self.settings.anilist_username.strip():
            return logs

        min_age_seconds = max(0, self.settings.refresh_interval_minutes) * 60
        if not force and not self.anilist.is_sync_due(min_age_seconds):
            age_seconds = self.anilist.seconds_since_last_sync() or 0.0
            remaining = min_age_seconds - age_seconds
            reason = "hunting" if hunting else "routine"
            self.anilist_sync_succeeded = None
            logs.append(
                f"AniList schedule sync deferred ({reason} pass): last synced {_format_age(age_seconds)} ago, "
                f"next due in {_format_age(remaining)} (refresh interval {self.settings.refresh_interval_minutes}m)."
            )
            if direct_mode:
                logs.extend(await self._sync_episode_airing_schedules())
            return logs

        existing_shows = {s.anilist_id: s for s in self.session.exec(select(Monitored)).all()}
        self.anilist_sync_succeeded = False
        for show in existing_shows.values():
            if not show.schedule_stale:
                show.schedule_stale = True
                self.session.add(show)
        try:
            seasonal_list = await self.anilist.fetch_user_seasonal_anime(
                self.settings.anilist_username,
                monitored_anilist_ids=set(existing_shows.keys()),
            )
        except AniListRateLimited as e:
            logger.warning(f"AniList rate limited; schedule sync deferred: {e}")
            self.session.commit()
            return logs + [f"AniList rate limited; schedule sync deferred: {e}"]
        except AniListError as e:
            logger.warning(f"Could not refresh AniList schedule: {e}")
            self.session.commit()
            return logs + [f"AniList schedule sync error: {e}"]
        self.anilist_sync_succeeded = True

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
                if show.anilist_status != (data.get("status") or None):
                    show.anilist_status = data.get("status") or None
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
                    if anilist_marked_complete or has_downloaded_final_episode(self.session, show, self.settings.download_mode):
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
                    and not has_downloaded_final_episode(self.session, show, self.settings.download_mode)
                ):
                    show.schedule_stale = True
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
        if direct_mode:
            logs.extend(await self._sync_episode_airing_schedules())
        return logs

    async def _sync_episode_airing_schedules(self) -> List[str]:
        """Refresh the authoritative per-episode air dates the direct engine needs.

        The direct engine gates every backfill on a known ``air_at``, so an
        episode without one is only grabbed when it is the freshest wanted
        episode. That makes a stale or missing per-episode schedule directly
        affect what gets downloaded, so it is refreshed on its own cadence
        rather than piggy-backing on the user-list query.
        """
        logs: List[str] = []
        now = utc_now()
        cutoff = now - timedelta(seconds=EPISODE_SCHEDULE_MAX_AGE_SECONDS)
        due = [
            show for show in self.session.exec(
                select(Monitored).where(
                    Monitored.status.in_([
                        MonitoredStatus.UNCONFIRMED,
                        MonitoredStatus.FIXED,
                        MonitoredStatus.STALLED,
                    ])
                )
            ).all()
            if show.schedule_synced_at is None or _aware(show.schedule_synced_at) < cutoff
        ]
        if not due:
            return logs

        fetch = getattr(self.anilist, "fetch_media_airing_schedules", None)
        if not inspect.iscoroutinefunction(fetch):
            return logs

        try:
            schedules = await fetch([show.anilist_id for show in due])
        except AniListRateLimited as e:
            logger.warning(f"AniList rate limited; episode schedule deferred: {e}")
            return [f"AniList rate limited; episode schedule deferred: {e}"]
        except AniListError as e:
            logger.warning(f"Could not refresh episode airing schedules: {e}")
            return [f"Could not refresh episode airing schedules: {e}"]

        missing: List[str] = []
        updated_shows = 0
        for show in due:
            entries = schedules.get(show.anilist_id)
            if entries is None:
                missing.append(show.display_name)
                continue
            sync_show_episodes(self.session, show)
            for entry in entries:
                episode = self.session.exec(
                    select(Episode).where(
                        Episode.monitored_id == show.id,
                        Episode.episode_number == int(entry["episode"]),
                    )
                ).first()
                if not episode:
                    continue
                air_at = entry.get("airing_at")
                if not air_at:
                    continue
                episode.air_at = air_at
                episode.schedule_state = (
                    EpisodeScheduleState.AIRED.name.lower()
                    if air_at <= now
                    else EpisodeScheduleState.SCHEDULED.name.lower()
                )
                self.session.add(episode)
            show.schedule_synced_at = now
            show.schedule_stale = False
            self.session.add(show)
            updated_shows += 1

        self.session.commit()
        if updated_shows:
            logs.append(f"Refreshed per-episode air dates for {updated_shows} show(s) from AniList.")
        if missing:
            logs.append(
                f"Warning: no AniList airing schedule for {len(missing)} show(s); "
                "their episodes stay unverified."
            )
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
        Deal with episodes whose authoritative air time has passed.

        In the direct engines the schedule is never invented: an overdue episode
        is recorded as aired against its real ``air_at`` and left there, because
        the episode ledger (and the releases matched to it) is what a download
        decision is made from. Guessing a weekly rollover would renumber a
        season against feeds that carry absolute numbering.

        In rules mode qBittorrent only follows ``next_airing_*``, so the weekly
        heuristic is kept there as a fallback for a lagging or unreachable
        AniList.
        """
        logs = []
        now = utc_now()
        active_stmt = select(Monitored).where(
            Monitored.status.in_([MonitoredStatus.UNCONFIRMED, MonitoredStatus.FIXED, MonitoredStatus.STALLED])
        )
        shows = self.session.exec(active_stmt).all()
        use_heuristic = self.download_mode() == "rules"

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
                if has_downloaded_final_episode(self.session, show, self.settings.download_mode):
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

            if not use_heuristic:
                episode = self.session.exec(
                    select(Episode).where(
                        Episode.monitored_id == show.id,
                        Episode.episode_number == current_ep,
                    )
                ).first()
                if episode is None:
                    episode = Episode(
                        monitored_id=show.id,
                        episode_number=current_ep,
                        air_at=air_at,
                    )
                    self.session.add(episode)
                if episode.schedule_state != EpisodeScheduleState.AIRED.name.lower():
                    episode.schedule_state = EpisodeScheduleState.AIRED.name.lower()
                    self.session.add(episode)
                msg = f"Show '{show.display_name}': Episode {current_ep} remains overdue at its authoritative air time."
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

            air_at = show.next_airing_at
            if air_at and air_at.tzinfo is None:
                air_at = air_at.replace(tzinfo=timezone.utc)

            # Non-completed shows from past seasons are never pruned
            if show.status != MonitoredStatus.COMPLETED:
                continue

            # Grace period check: if next_airing_at was recent, the release may still be pending
            if air_at and air_at > (now - timedelta(days=7)):
                continue

            # Check episode ledger: if any episode is not COMPLETED, do not prune
            episodes = self.session.exec(
                select(Episode).where(
                    Episode.monitored_id == show.id,
                    Episode.episode_number <= (show.total_episodes or 9999),
                )
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

    def shield_owned_articles(
        self,
        articles_by_url: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> List[str]:
        logs: List[str] = []
        for show in self.session.exec(select(Monitored)).all():
            sync_show_episodes(self.session, show)
        owned_titles_by_show: Dict[int, set] = {}
        feed_urls = {feed.id: feed.qbit_feed_url for feed in self.session.exec(select(Feed)).all()}
        for show in self.session.exec(select(Monitored)).all():
            titles = {
                episode.release_title
                for episode in self.session.exec(select(Episode).where(
                    Episode.monitored_id == show.id,
                    Episode.status.in_([
                        EpisodeStatus.QUEUED,
                        EpisodeStatus.DOWNLOADING,
                        EpisodeStatus.COMPLETED,
                        EpisodeStatus.REPLACING,
                    ]),
                )).all()
                if episode.release_title
                and (
                    episode.status == EpisodeStatus.COMPLETED
                    or bool(episode.torrent_hash)
                )
            }
            history_titles = {
                history.release_title
                for history in self.session.exec(select(MatchHistory).where(
                    MatchHistory.monitored_id == show.id,
                )).all()
                if history.release_title
            }
            titles |= history_titles
            if titles and show.id:
                owned_titles_by_show[show.id] = titles
        if not owned_titles_by_show:
            return logs
        try:
            feed_paths = self.qbit.get_rss_feed_paths()
        except Exception as e:
            logger.debug(f"RSS article shielding skipped: {e}")
            return logs
        if not feed_paths:
            return logs
        if articles_by_url is None:
            try:
                articles_by_url = flatten_rss_articles(self.qbit.get_rss_items(with_data=True))
            except Exception as e:
                logger.debug(f"RSS article shielding cache unavailable: {e}")
                return logs
        shielded_keys = {
            (row.feed_url, row.item_id)
            for row in self.session.exec(
                select(SeenFeedItem).where(SeenFeedItem.shielded_at.is_not(None))
            ).all()
        }
        shielded_count = 0
        shielded_shows = set()
        for show in self.session.exec(select(Monitored)).all():
            if not show.current_feed_id:
                continue
            titles = owned_titles_by_show.get(show.id, set())
            if not titles:
                continue
            feed_url = feed_urls.get(show.current_feed_id)
            if not feed_url or feed_url not in feed_paths:
                continue
            item_path = feed_paths[feed_url]
            for article in articles_by_url.get(feed_url, []):
                title = str(article.get("title") or "")
                if title not in titles:
                    continue
                item_id = article.get("id") or article.get("torrentURL") or article.get("link")
                if not item_id:
                    continue
                item_id = str(item_id)
                key = (feed_url, item_id)
                if key in shielded_keys:
                    continue
                try:
                    self.qbit.mark_rss_article_read(item_path, item_id)
                    existing = self.session.exec(
                        select(SeenFeedItem).where(
                            SeenFeedItem.feed_url == feed_url,
                            SeenFeedItem.item_id == item_id,
                        )
                    ).first()
                    if existing:
                        existing.title = title
                        existing.shielded_at = utc_now()
                        self.session.add(existing)
                    else:
                        self.session.add(SeenFeedItem(
                            feed_url=feed_url,
                            item_id=item_id,
                            title=title,
                            shielded_at=utc_now(),
                        ))
                    self.session.commit()
                    shielded_keys.add(key)
                    shielded_count += 1
                    shielded_shows.add(show.display_name)
                except Exception as e:
                    logger.debug(f"Could not shield owned RSS article: {e}")
        if shielded_count:
            show_count = len(shielded_shows)
            logs.append(
                f"Marked {shielded_count} previously downloaded RSS release(s) as read across {show_count} show(s)."
            )
        return logs

    def _reassign_direct_feeds(self, all_feeds: List[Feed]) -> List[str]:
        logs: List[str] = []
        now = utc_now()
        try:
            cached_articles = RssSnapshot(self.qbit).get()
        except QbitClientError as e:
            logger.debug(f"Feed reassignment skipped: {e}")
            return logs

        downloaded_show_ids = {
            show_id for show_id in self.session.exec(
                select(Episode.monitored_id).where(
                    Episode.feed_id.is_not(None),
                    Episode.torrent_hash.is_not(None),
                )
            ).all() if show_id
        }
        candidate_shows = self.session.exec(
            select(Monitored).where(
                Monitored.current_feed_id.is_not(None),
                Monitored.feed_pinned == False,  # noqa: E712
                Monitored.status.in_([MonitoredStatus.UNCONFIRMED, MonitoredStatus.STALLED]),
            )
        ).all()

        for show in candidate_shows:
            if show.id in downloaded_show_ids:
                show.candidate_feed_id = None
                show.candidate_feed_since = None
                self.session.add(show)
                continue

            res = discover_feed_for_show(
                monitored=show,
                feeds=[feed for feed in all_feeds if feed.id != show.current_feed_id],
                qbit_client=self.qbit,
                excluded_feed_ids=[],
                cached_articles_by_url=cached_articles,
                rss_snapshot=None,
                parsed_articles={},
            )
            if not res:
                continue
            chosen_feed = res[0]
            if show.candidate_feed_id != chosen_feed.id:
                show.candidate_feed_id = chosen_feed.id
                show.candidate_feed_since = now
                self.session.add(show)
                self.session.commit()
                continue
            since = show.candidate_feed_since
            if since is None or (since.tzinfo is None and since.replace(tzinfo=timezone.utc) + timedelta(
                seconds=FEED_SWITCH_GRACE_SECONDS
            ) > now):
                continue
            show.current_feed_id = chosen_feed.id
            show.candidate_feed_id = None
            show.candidate_feed_since = None
            self.session.add(show)
            self.session.commit()
            msg = (
                f"Assigned '{show.display_name}' to feed '{chosen_feed.qbit_feed_name}': "
                f"'{all_feeds[0].qbit_feed_name}' never carried a release for it."
            )
            logger.info(msg)
            logs.append(msg)
        return logs

    def disable_managed_rules(self) -> List[str]:
        logs: List[str] = []
        rules = self.qbit.get_rss_rules()
        shows = self.session.exec(select(Monitored)).all()
        owned_names = {
            rule_name
            for rule_name in rules
            if rule_name.startswith("[Seasonal]")
            or rule_name.startswith("[qbit-seasonal-anime]")
            or any(
                show.qbit_rule_name == rule_name for show in shows
            )
        }
        failed: List[str] = []
        for rule_name in sorted(owned_names):
            rule_def = rules.get(rule_name, {})
            if rule_def.get("enabled") is False:
                continue
            try:
                updated = dict(rule_def)
                updated["enabled"] = False
                self.qbit.set_rss_rule(rule_name, updated)
                logs.append(f"Disabled managed RSS rule '{rule_name}'.")
            except Exception as e:
                failed.append(rule_name)
                logger.warning(f"Could not disable managed RSS rule '{rule_name}': {e}")
        current_rules = self.qbit.get_rss_rules()
        remaining = [
            rule_name
            for rule_name in owned_names
            if current_rules.get(rule_name, {}).get("enabled", False) is not False
        ]
        if remaining or failed:
            raise QbitClientError(f"Managed RSS rules remain active: {', '.join(remaining or failed)}")
        return logs

    def shield_owned_articles_from_snapshot(self, rss_snapshot: RssSnapshot) -> List[str]:
        return self.shield_owned_articles(rss_snapshot.get())

    def import_existing_torrents(self) -> List[str]:
        """Adopt torrents already in qBittorrent into the canonical model.

        Direct mode owns its library, so anything already downloaded must be
        known before the pipeline decides what is still missing.
        """
        logs: List[str] = []
        try:
            torrents = list(self.qbit.get_torrents(tag="qsa-managed"))
        except Exception as e:
            logger.warning(f"Could not read managed torrents for import: {e}")
            return [f"Could not import existing torrents: {e}"]
        if not torrents:
            try:
                torrents = list(self.qbit.get_torrents(category=self.settings.default_category))
            except Exception as e:
                logger.warning(f"Could not read category torrents for import: {e}")
                return []
        known_hashes = {
            episode.torrent_hash
            for episode in self.session.exec(select(Episode).where(Episode.torrent_hash.is_not(None))).all()
        }
        shows = self.session.exec(
            select(Monitored).where(
                Monitored.status.in_([
                    MonitoredStatus.UNCONFIRMED,
                    MonitoredStatus.FIXED,
                    MonitoredStatus.STALLED,
                ])
            )
        ).all()
        candidates = [(show, prepare_aliases(show.aliases)) for show in shows]
        parsed_cache: Dict[str, Dict[str, Any]] = {}
        imported = 0
        for torrent in torrents:
            torrent_hash = str(getattr(torrent, "hash", "") or "")
            name = str(getattr(torrent, "name", "") or "")
            if not torrent_hash or not name or torrent_hash in known_hashes:
                continue
            parsed = parse_release_title(name)
            raw_episode = parsed.get("episode")
            if raw_episode is None:
                continue
            matched_show = None
            for show, prepared in candidates:
                if match_release_to_show(
                    name,
                    show.aliases,
                    prepared_aliases=prepared,
                    parsed_cache=parsed_cache,
                    ignore_arc_marker=False,
                )[0]:
                    matched_show = show
                    break
            if not matched_show or not matched_show.id:
                continue
            episodes = sync_show_episodes(self.session, matched_show)
            episode = next(
                (item for item in episodes if item.episode_number == int(raw_episode)),
                None,
            )
            if not episode:
                continue
            episode.release_title = name
            episode.release_group = parsed.get("release_group")
            episode.torrent_hash = torrent_hash
            episode.source_episode = int(raw_episode)
            episode.feed_id = matched_show.current_feed_id
            episode.version = max(1, int(parsed.get("version") or 1))
            progress = float(getattr(torrent, "progress", 0) or 0)
            state_name = str(getattr(torrent, "state", "") or "").lower()
            complete = progress >= 1 or state_name in {
                "uploading", "pausedup", "stoppedup", "queuedup", "forcedup", "stalledup",
            }
            episode.status = EpisodeStatus.COMPLETED if complete else EpisodeStatus.DOWNLOADING
            if complete:
                episode.downloaded_at = episode.downloaded_at or utc_now()
            known_hashes.add(torrent_hash)
            self.session.add(episode)
            imported += 1
        if imported:
            self.session.commit()
            logs.append(f"Imported {imported} existing torrent(s) into the episode library.")
        return logs

    def prepare_download_mode(self, mode: str) -> List[str]:
        if mode in {"direct", "observe"}:
            logs = self.import_existing_torrents()
            logs.extend(self.disable_managed_rules())
            return logs
        logs: List[str] = []
        snapshot = RssSnapshot(self.qbit)
        logs.extend(self.shield_owned_articles())
        logs.extend(self.bootstrap_unassigned_shows(
            rss_snapshot=snapshot,
            create_qbit_rules=True,
        ))
        logs.extend(self.sync_active_rules())
        return logs

    async def run_full_cycle(
        self,
        *,
        hunting: bool = False,
        force_rss_refresh: bool = False,
    ) -> List[str]:
        """Execute one complete supervision iteration.

        A cross-process lease keeps the daemon and a manually triggered cycle
        from running against the same state at once.

        ``hunting`` marks a short release-polling pass for the AniList sync log
        line. ``force_rss_refresh`` makes the direct engine re-read the feeds
        before deciding, because a stale snapshot would otherwise be acted upon.
        """
        engine = self.session.get_bind()
        owner = f"supervisor:{uuid4().hex}"
        if not acquire_supervision_lease(engine, owner):
            raise QbitClientError("Another supervision cycle holds the database lease")
        try:
            return await self._run_full_cycle(
                hunting=hunting,
                force_rss_refresh=force_rss_refresh,
                lease_owner=owner,
            )
        except Exception:
            try:
                self.session.rollback()
            except Exception as e:
                logger.warning(f"Could not roll back after a failed cycle: {e}")
            raise
        finally:
            release_supervision_lease(engine, owner)

    async def _run_full_cycle(
        self,
        *,
        hunting: bool = False,
        force_rss_refresh: bool = False,
        lease_owner: Optional[str] = None,
    ) -> List[str]:
        all_logs: List[str] = []
        rss_snapshot = RssSnapshot(self.qbit)
        parsed_articles: Dict[str, Dict[str, Any]] = {}
        self._known_categories.clear()
        engine = self.session.get_bind()

        def beat() -> None:
            if lease_owner and not heartbeat_supervision_lease(engine, lease_owner):
                raise QbitClientError("Supervision lease was lost during the cycle")

        mode = self.download_mode()

        all_logs.extend(await asyncio.to_thread(self.sync_feeds))

        beat()
        all_logs.extend(await self.sync_anilist_schedule(
            hunting=hunting,
            direct_mode=mode in {"direct", "observe"},
        ))

        beat()
        if mode in {"direct", "observe"}:
            try:
                all_logs.extend(await asyncio.to_thread(self.prepare_download_mode, mode))
            except QbitClientError:
                raise
            except Exception as e:
                raise QbitClientError(f"{mode.title()} mode preflight failed: {e}") from e

        if mode == "direct":
            if force_rss_refresh:
                all_logs.extend(await asyncio.to_thread(
                    update_episode_status,
                    self.session,
                    self.qbit,
                    self.settings,
                ))
                refreshed_articles = await asyncio.to_thread(rss_snapshot.refresh)
                all_logs.append("Refreshed qBittorrent RSS feeds before direct evaluation.")
                expected_urls = {
                    feed.qbit_feed_url for feed in self.session.exec(select(Feed)).all()
                }
                missing_feeds = expected_urls - set(refreshed_articles)
                if rss_snapshot.failed_feed_names:
                    all_logs.append(
                        "Warning: RSS feeds unavailable after refresh: "
                        + ", ".join(rss_snapshot.failed_feed_names)
                    )
                if missing_feeds:
                    all_logs.append(
                        "Warning: RSS feeds missing from the refreshed response: "
                        + str(len(missing_feeds))
                    )
            all_logs.extend(await asyncio.to_thread(
                self.bootstrap_unassigned_shows,
                rss_snapshot,
                parsed_articles,
                self._known_categories,
                False,
            ))
            beat()
            all_logs.extend(await asyncio.to_thread(
                evaluate_and_grab_releases,
                self.session,
                self.qbit,
                self.settings,
                None,
                "direct",
                rss_snapshot,
            ))
            beat()
            if self.anilist_sync_succeeded is not False:
                all_logs.extend(await asyncio.to_thread(self.reconcile_schedule_rollover))
        else:
            all_logs.extend(await asyncio.to_thread(
                self.shield_owned_articles_from_snapshot,
                rss_snapshot,
            ))
            all_logs.extend(await asyncio.to_thread(
                self.bootstrap_unassigned_shows,
                rss_snapshot,
                parsed_articles,
                self._known_categories,
                mode == "rules",
                mode == "rules",
            ))
            if mode == "observe":
                all_logs.extend(await asyncio.to_thread(
                    evaluate_and_grab_releases,
                    self.session,
                    self.qbit,
                    self.settings,
                    None,
                    "observe",
                    rss_snapshot,
                ))
            else:
                all_logs.extend(await asyncio.to_thread(
                    verify_and_confirm_torrents,
                    self.session,
                    self.qbit,
                    self.settings,
                    rss_snapshot,
                    parsed_articles,
                    self._known_categories,
                ))
            if self.anilist_sync_succeeded is not False:
                all_logs.extend(await asyncio.to_thread(self.reconcile_schedule_rollover))
            if mode == "rules":
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

        beat()
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
        wanted_cnt = self.session.exec(
            select(func.count(Episode.id)).where(Episode.status == EpisodeStatus.WANTED)
        ).one()
        if wanted_cnt:
            summary += f" | {wanted_cnt} Wanted"
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
