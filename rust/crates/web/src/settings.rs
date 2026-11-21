//! The user's settings: one validated shape for the Settings page and the first-run wizard.

use std::collections::BTreeSet;

use kisetsu_db::schema::{Settings, Show};
use kisetsu_domain::patterns::compress_home_path;
use kisetsu_services::context::{Context, setup_completed};
use kisetsu_services::{Error, Result, library, tags, watched};
use serde_json::{Value, json};

use crate::check::Reader;

/// The least and most minutes between reads of the feeds.
const FEED_REFRESH_MINUTES: (i64, i64) = (5, 240);
/// The least and most minutes between the hunt's looks.
const HUNT_INTERVAL_MINUTES: (i64, i64) = (2, 60);
/// The resolutions a minimum may be.
const MIN_RESOLUTIONS: [i64; 3] = [480, 720, 1080];
const TINTS: [&str; 3] = ["off", "subtle", "full"];
pub const LANGUAGES: [&str; 2] = ["english", "romaji"];
/// The wizard may only set these; the rest of the page opens once setup is finished.
pub const SETUP_FIELDS: [&str; 11] = [
    "qbit_host",
    "qbit_username",
    "qbit_password",
    "base_dir",
    "default_category",
    "default_seed_ratio",
    "title_language",
    "accent_color",
    "accent_tint",
    "desktop_notifications",
    "delete_watched",
];

/// Any subset of the settings. A field left out, or sent as `null`, stays as it is.
#[derive(Debug, Default, Clone)]
pub struct SettingsPatch {
    pub qbit_host: Option<String>,
    pub qbit_username: Option<String>,
    pub qbit_password: Option<String>,
    pub base_dir: Option<String>,
    pub default_category: Option<String>,
    pub default_seed_ratio: Option<f64>,
    pub anilist_username: Option<String>,
    pub anilist_sync_minutes: Option<i64>,
    pub feed_refresh_minutes: Option<i64>,
    pub hunt_interval_minutes: Option<i64>,
    pub early_air_tolerance_hours: Option<i64>,
    pub title_language: Option<String>,
    pub move_existing: Option<bool>,
    pub accent_color: Option<String>,
    pub accent_tint: Option<String>,
    pub desktop_notifications: Option<bool>,
    pub min_resolution: Option<i64>,
    pub delete_watched: Option<bool>,
    /// With `delete_watched` turned on: also delete the torrents of the episodes already
    /// watched.
    pub delete_watched_now: Option<bool>,
}

const PATCH_FIELDS: [&str; 19] = [
    "qbit_host",
    "qbit_username",
    "qbit_password",
    "base_dir",
    "default_category",
    "default_seed_ratio",
    "anilist_username",
    "anilist_sync_minutes",
    "feed_refresh_minutes",
    "hunt_interval_minutes",
    "early_air_tolerance_hours",
    "title_language",
    "move_existing",
    "accent_color",
    "accent_tint",
    "desktop_notifications",
    "min_resolution",
    "delete_watched",
    "delete_watched_now",
];

fn in_range(
    reader: &mut Reader<'_>,
    name: &str,
    value: Option<i64>,
    (least, most): (i64, i64),
) -> Option<i64> {
    let value = value?;
    if value < least {
        reader.refuse_below(name, value, least);
        return None;
    }
    if value > most {
        reader.refuse_above(name, value, most);
        return None;
    }
    Some(value)
}

fn is_color(text: &str) -> bool {
    text.len() == 7 && text.starts_with('#') && text[1..].chars().all(|c| c.is_ascii_hexdigit())
}

impl SettingsPatch {
    /// Reads the patch from a body, noting every problem in `reader`.
    pub fn read(reader: &mut Reader<'_>, body: &Value) -> Self {
        let mut patch = Self {
            qbit_host: reader.optional_string(body, "qbit_host"),
            qbit_username: reader.optional_string(body, "qbit_username"),
            qbit_password: reader.optional_string(body, "qbit_password"),
            base_dir: reader.optional_string(body, "base_dir"),
            default_category: reader.optional_string(body, "default_category"),
            anilist_username: reader.optional_string(body, "anilist_username"),
            move_existing: reader.optional_flag(body, "move_existing"),
            desktop_notifications: reader.optional_flag(body, "desktop_notifications"),
            delete_watched: reader.optional_flag(body, "delete_watched"),
            delete_watched_now: reader.optional_flag(body, "delete_watched_now"),
            ..Self::default()
        };
        patch.default_seed_ratio = reader.optional_float(body, "default_seed_ratio");
        if let Some(ratio) = patch.default_seed_ratio.filter(|ratio| *ratio < 0.0) {
            reader.refuse_below_number("default_seed_ratio", ratio, 0);
            patch.default_seed_ratio = None;
        }
        let minutes = reader.optional_integer(body, "anilist_sync_minutes");
        patch.anilist_sync_minutes = in_range(reader, "anilist_sync_minutes", minutes, (1, 10080));
        let minutes = reader.optional_integer(body, "feed_refresh_minutes");
        patch.feed_refresh_minutes = in_range(
            reader,
            "feed_refresh_minutes",
            minutes,
            FEED_REFRESH_MINUTES,
        );
        let minutes = reader.optional_integer(body, "hunt_interval_minutes");
        patch.hunt_interval_minutes = in_range(
            reader,
            "hunt_interval_minutes",
            minutes,
            HUNT_INTERVAL_MINUTES,
        );
        let hours = reader.optional_integer(body, "early_air_tolerance_hours");
        patch.early_air_tolerance_hours =
            in_range(reader, "early_air_tolerance_hours", hours, (0, 168));
        patch.min_resolution = reader.optional_integer(body, "min_resolution");
        if let Some(value) = patch
            .min_resolution
            .filter(|value| !MIN_RESOLUTIONS.contains(value))
        {
            let allowed = MIN_RESOLUTIONS.map(|value| value.to_string()).join(", ");
            reader.refuse_value(
                "min_resolution",
                &value.to_string(),
                &format!("must be one of {allowed}"),
            );
            patch.min_resolution = None;
        }
        patch.title_language = lowered(reader, body, "title_language", &LANGUAGES);
        patch.accent_tint = lowered(reader, body, "accent_tint", &TINTS);
        if let Some(color) = reader.optional_string(body, "accent_color") {
            let color = color.trim().to_owned();
            if is_color(&color) {
                patch.accent_color = Some(color.to_lowercase());
            } else {
                reader.refuse_value("accent_color", &color, "must be a #rrggbb colour");
            }
        }
        reader.forbid_others(body, &PATCH_FIELDS);
        patch
    }

    /// The names of the fields the patch sets.
    pub fn given(&self) -> BTreeSet<&'static str> {
        let set = [
            ("qbit_host", self.qbit_host.is_some()),
            ("qbit_username", self.qbit_username.is_some()),
            ("qbit_password", self.qbit_password.is_some()),
            ("base_dir", self.base_dir.is_some()),
            ("default_category", self.default_category.is_some()),
            ("default_seed_ratio", self.default_seed_ratio.is_some()),
            ("anilist_username", self.anilist_username.is_some()),
            ("anilist_sync_minutes", self.anilist_sync_minutes.is_some()),
            ("feed_refresh_minutes", self.feed_refresh_minutes.is_some()),
            (
                "hunt_interval_minutes",
                self.hunt_interval_minutes.is_some(),
            ),
            (
                "early_air_tolerance_hours",
                self.early_air_tolerance_hours.is_some(),
            ),
            ("title_language", self.title_language.is_some()),
            ("move_existing", self.move_existing.is_some()),
            ("accent_color", self.accent_color.is_some()),
            ("accent_tint", self.accent_tint.is_some()),
            (
                "desktop_notifications",
                self.desktop_notifications.is_some(),
            ),
            ("min_resolution", self.min_resolution.is_some()),
            ("delete_watched", self.delete_watched.is_some()),
            ("delete_watched_now", self.delete_watched_now.is_some()),
        ];
        set.into_iter()
            .filter(|(_, given)| *given)
            .map(|(name, _)| name)
            .collect()
    }
}

/// A text of a fixed set of words, stripped and lowered.
fn lowered(reader: &mut Reader<'_>, body: &Value, name: &str, allowed: &[&str]) -> Option<String> {
    let value = reader.optional_string(body, name)?;
    let lowered = value.trim().to_lowercase();
    if allowed.contains(&lowered.as_str()) {
        Some(lowered)
    } else {
        reader.refuse_value(name, &value, &format!("must be {}", allowed.join(" or ")));
        None
    }
}

/// The stored form of a base directory: it always ends in the `{name}` placeholder unless blank.
pub fn normalize_base_dir(raw: &str) -> String {
    let value = raw.trim();
    if !value.is_empty() && !value.contains("{name}") {
        format!("{}/{{name}}", value.trim_end_matches(['/', '\\']))
    } else {
        value.to_owned()
    }
}

/// The settings as the page shows them; the password is never sent.
pub fn settings_view(settings: &Settings) -> Value {
    let mut base_dir = settings.base_dir.clone();
    if !base_dir.is_empty() && !base_dir.contains("{name}") {
        base_dir = format!("{}/{{name}}", base_dir.trim_end_matches('/'));
    }
    let home = std::env::home_dir().unwrap_or_default();
    json!({
        "qbit_host": settings.qbit_host,
        "qbit_username": settings.qbit_username,
        "qbit_password_set": !settings.qbit_password.is_empty(),
        "base_dir": compress_home_path(&base_dir, &home.to_string_lossy()),
        "default_category": settings.default_category,
        "default_seed_ratio": settings.default_seed_ratio,
        "anilist_username": settings.anilist_username,
        "anilist_sync_minutes": settings.anilist_sync_minutes,
        "feed_refresh_minutes": settings.feed_refresh_minutes,
        "hunt_interval_minutes": settings.hunt_interval_minutes,
        "early_air_tolerance_hours": settings.early_air_tolerance_hours,
        "title_language": settings.title_language,
        "accent_color": settings.accent_color,
        "accent_tint": settings.accent_tint,
        "desktop_notifications": settings.desktop_notifications,
        "min_resolution": settings.min_resolution,
        "delete_watched": settings.delete_watched,
    })
}

/// The stored password may only go to the stored host, so a new host needs it typed again.
///
/// Until the first-run wizard is finished nothing the user chose is stored yet (only the default
/// login), so the wizard can try a new address without a password, for a qBittorrent that needs
/// none on this machine.
pub fn check_password_travels_with_host(
    ctx: &Context<'_>,
    host: &str,
    password: Option<&str>,
) -> Result<()> {
    let settings = &ctx.settings;
    if !setup_completed(ctx.conn)? {
        return Ok(());
    }
    let same = host.trim_end_matches('/') == settings.qbit_host.trim_end_matches('/');
    if !same && !settings.qbit_password.is_empty() && password.is_none_or(str::is_empty) {
        return Err(Error::Invalid(
            "Enter the password to use a different qBittorrent host.".to_owned(),
        ));
    }
    Ok(())
}

fn moving_error(moving: usize) -> Error {
    Error::Invalid(format!(
        "{moving} downloads are still moving to their folders. Try again in a moment."
    ))
}

/// The folder renames a language switch causes, refusing while qBittorrent is still moving some.
pub fn move_plan(ctx: &mut Context<'_>, language: &str) -> Result<Vec<library::MovePlan>> {
    let plan = library::plan_language_move(ctx, language)?;
    let moving: usize = plan.iter().map(|item| item.moving).sum();
    if moving > 0 {
        return Err(moving_error(moving));
    }
    Ok(plan)
}

/// The folder moves this patch causes, and whether qBittorrent could not be asked.
///
/// A new base folder always moves the downloads; a new title language only when asked to.
/// Planned before anything changes, since the plan reads where the downloads are now, and
/// refused while qBittorrent is still moving some.
fn patch_moves(
    ctx: &mut Context<'_>,
    patch: &SettingsPatch,
) -> Result<(Vec<library::MovePlan>, bool)> {
    let current = ctx.settings.clone();
    let base_dir = patch
        .base_dir
        .as_deref()
        .map_or_else(|| current.base_dir.clone(), normalize_base_dir);
    let language = patch.title_language.as_deref();
    let rename = language.is_some_and(|language| language != current.title_language)
        && patch.move_existing == Some(true);
    if base_dir == current.base_dir && !rename {
        return Ok((Vec::new(), false));
    }
    let new_language = match language {
        Some(language) if rename => language.to_owned(),
        _ => current.title_language.clone(),
    };
    let mut moved = current.clone();
    moved.base_dir.clone_from(&base_dir);
    let shows = Show::select(ctx.conn, "ORDER BY id", [])?;
    let anywhere = base_dir != current.base_dir;
    let plan = library::plan_move(
        ctx,
        &shows,
        |show| library::save_path(&moved, show, &new_language),
        anywhere,
    );
    let plan = match plan {
        Ok(plan) => plan,
        Err(Error::Qbit(error)) => {
            if rename {
                return Err(Error::Qbit(error));
            }
            return Ok((Vec::new(), true));
        }
        Err(other) => return Err(other),
    };
    let moving: usize = plan.iter().map(|item| item.moving).sum();
    if moving > 0 {
        return Err(moving_error(moving));
    }
    Ok((plan, false))
}

/// Stores the patch; returns the log lines of a folder move the title language asked for.
pub fn apply_settings(ctx: &mut Context<'_>, patch: &SettingsPatch) -> Result<Vec<String>> {
    let (plan, unreachable) = patch_moves(ctx, patch)?;
    if let Some(host) = &patch.qbit_host {
        let host = host.trim().to_owned();
        check_password_travels_with_host(ctx, &host, patch.qbit_password.as_deref())?;
        ctx.settings.qbit_host = host;
    }
    if let Some(name) = &patch.qbit_username {
        ctx.settings.qbit_username = name.trim().to_owned();
    }
    if let Some(password) = patch
        .qbit_password
        .as_ref()
        .filter(|password| !password.is_empty())
    {
        ctx.settings.qbit_password.clone_from(password);
    }
    if let Some(base_dir) = &patch.base_dir {
        ctx.settings.base_dir = normalize_base_dir(base_dir);
    }
    if let Some(category) = &patch.default_category {
        ctx.settings.default_category = category.trim().to_owned();
    }
    let s = &mut ctx.settings;
    if let Some(ratio) = patch.default_seed_ratio {
        s.default_seed_ratio = ratio;
    }
    if let Some(value) = patch.anilist_sync_minutes {
        s.anilist_sync_minutes = value;
    }
    if let Some(value) = patch.feed_refresh_minutes {
        s.feed_refresh_minutes = value;
    }
    if let Some(value) = patch.hunt_interval_minutes {
        s.hunt_interval_minutes = value;
    }
    if let Some(value) = patch.early_air_tolerance_hours {
        s.early_air_tolerance_hours = value;
    }
    if let Some(value) = &patch.accent_color {
        s.accent_color.clone_from(value);
    }
    if let Some(value) = &patch.accent_tint {
        s.accent_tint.clone_from(value);
    }
    if let Some(value) = patch.desktop_notifications {
        s.desktop_notifications = value;
    }
    if let Some(value) = patch.min_resolution {
        s.min_resolution = value;
    }
    if let Some(name) = &patch.anilist_username {
        s.anilist_username = name.trim().to_owned();
    }
    if let Some(on) = patch
        .delete_watched
        .filter(|on| *on != ctx.settings.delete_watched)
    {
        ctx.settings.delete_watched = on;
        ctx.settings.update(ctx.conn)?;
        watched::switched(ctx, patch.delete_watched_now == Some(true))?;
    }
    if let Some(language) = patch
        .title_language
        .as_ref()
        .filter(|language| **language != ctx.settings.title_language)
    {
        ctx.settings.title_language.clone_from(language);
        ctx.settings.update(ctx.conn)?;
        tags::sync_tags(ctx)?;
    }
    ctx.settings.update(ctx.conn)?;
    ctx.emit(&["settings", "shows"]);
    if unreachable {
        return Ok(vec![
            "qBittorrent could not be reached, so the downloads already made stayed in their old folders."
                .to_owned(),
        ]);
    }
    if plan.is_empty() {
        return Ok(Vec::new());
    }
    ctx.checkpoint()?;
    let mut done = library::relocate_torrents(ctx, &plan)?;
    if !done.failed.is_empty() {
        done.logs.push(format!(
            "Some downloads stayed in their old folders: {}.",
            done.failed.join(", ")
        ));
    }
    Ok(done.logs)
}
