//! Enums that the database stores by member name. A member is never removed: rows holding it
//! would no longer load.

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum EpisodeStatus {
    Wanted,
    Downloading,
    Completed,
    Failed,
    Missed,
    /// The user chose not to download it; nothing grabs it until they want it again.
    Skipped,
}
