//! Builds the UI's files into the program: a table of each file's path under `/static`, its bytes
//! and the first ten hex digits of its SHA-1.

#![expect(
    clippy::panic,
    reason = "a build that cannot read the UI's files must stop"
)]

use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::{env, fs};

use sha1_smol::Sha1;

fn collect(folder: &Path, found: &mut Vec<PathBuf>) {
    let mut entries: Vec<PathBuf> = fs::read_dir(folder)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", folder.display()))
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<Result<_, _>>()
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", folder.display()));
    entries.sort();
    for path in entries {
        if path.is_dir() {
            collect(&path, found);
        } else {
            found.push(path);
        }
    }
}

fn main() {
    let manifest = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap_or_default());
    let root = manifest.join("../../../kisetsu/web/static");
    let root = fs::canonicalize(&root)
        .unwrap_or_else(|error| panic!("cannot find the UI at {}: {error}", root.display()));
    println!("cargo:rerun-if-changed={}", root.display());

    let mut files = Vec::new();
    collect(&root, &mut files);
    let mut table = String::from("pub(crate) static EMBEDDED: &[(&str, &[u8], &str)] = &[\n");
    for path in &files {
        println!("cargo:rerun-if-changed={}", path.display());
        let relative = path
            .strip_prefix(&root)
            .unwrap_or(path)
            .components()
            .map(|part| part.as_os_str().to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .join("/");
        let bytes = fs::read(path)
            .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
        let digest: String = Sha1::from(bytes).digest().to_string()[..10].to_owned();
        let _ = writeln!(
            table,
            "    ({relative:?}, include_bytes!({:?}), {digest:?}),",
            path.display().to_string()
        );
    }
    table.push_str("];\n");
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap_or_default()).join("embedded.rs");
    fs::write(&out, table)
        .unwrap_or_else(|error| panic!("cannot write {}: {error}", out.display()));
}
