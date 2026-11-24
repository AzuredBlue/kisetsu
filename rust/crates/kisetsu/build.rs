//! Builds the program's icon and manifest into the Windows executable. The icon is the UI's
//! `favicon.ico`, so the program, the tray and the browser tab share one file.

#![expect(clippy::panic, reason = "a build that cannot embed the icon must stop")]

use std::path::PathBuf;
use std::{env, fs};

fn main() {
    let manifest_dir = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap_or_default());
    let icon = manifest_dir.join("../../../kisetsu/web/static/favicon.ico");
    let manifest = manifest_dir.join("kisetsu.manifest");
    println!("cargo:rerun-if-changed={}", icon.display());
    println!("cargo:rerun-if-changed={}", manifest.display());
    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }

    let absolute = |path: &PathBuf| {
        fs::canonicalize(path)
            .unwrap_or_else(|error| panic!("cannot find {}: {error}", path.display()))
            .display()
            .to_string()
            .trim_start_matches(r"\\?\")
            .replace('\\', "/")
    };
    // Icon 1 is the program's icon and the one the tray loads; resource type 24 with id 1 is the
    // manifest Windows applies when it starts the program.
    let script = format!(
        "1 ICON \"{}\"\n1 24 \"{}\"\n",
        absolute(&icon),
        absolute(&manifest)
    );
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap_or_default()).join("kisetsu.rc");
    fs::write(&out, script)
        .unwrap_or_else(|error| panic!("cannot write {}: {error}", out.display()));
    if let Err(error) = embed_resource::compile(&out, embed_resource::NONE).manifest_required() {
        panic!("cannot embed the icon: {error}");
    }
}
