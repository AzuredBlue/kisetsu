//! Reading the file list out of a `.torrent` file (bencode).

use std::collections::BTreeMap;

/// How deeply lists and dictionaries may nest; a real torrent needs about five levels.
const MAX_DEPTH: usize = 16;

enum Value<'a> {
    Int,
    Bytes(&'a [u8]),
    List(Vec<Value<'a>>),
    Dict(BTreeMap<&'a [u8], Value<'a>>),
}

/// The number before `end` at `*at`, which moves past `end`.
fn number(data: &[u8], at: &mut usize, end: u8) -> Option<usize> {
    let stop = data.get(*at..)?.iter().position(|byte| *byte == end)? + *at;
    let text = std::str::from_utf8(data.get(*at..stop)?).ok()?;
    *at = stop + 1;
    text.parse().ok()
}

fn bytes<'a>(data: &'a [u8], at: &mut usize) -> Option<&'a [u8]> {
    let len = number(data, at, b':')?;
    let end = at.checked_add(len)?;
    let text = data.get(*at..end)?;
    *at = end;
    Some(text)
}

fn value<'a>(data: &'a [u8], at: &mut usize, depth: usize) -> Option<Value<'a>> {
    if depth > MAX_DEPTH {
        return None;
    }
    match *data.get(*at)? {
        b'i' => {
            *at += 1;
            let stop = data.get(*at..)?.iter().position(|byte| *byte == b'e')? + *at;
            let text = std::str::from_utf8(data.get(*at..stop)?).ok()?;
            text.parse::<i64>().ok()?;
            *at = stop + 1;
            Some(Value::Int)
        }
        b'l' => {
            *at += 1;
            let mut items = Vec::new();
            while *data.get(*at)? != b'e' {
                items.push(value(data, at, depth + 1)?);
            }
            *at += 1;
            Some(Value::List(items))
        }
        b'd' => {
            *at += 1;
            let mut entries = BTreeMap::new();
            while *data.get(*at)? != b'e' {
                let key = bytes(data, at)?;
                entries.insert(key, value(data, at, depth + 1)?);
            }
            *at += 1;
            Some(Value::Dict(entries))
        }
        b'0'..=b'9' => bytes(data, at).map(Value::Bytes),
        _ => None,
    }
}

fn text(value: &Value<'_>) -> Option<String> {
    match value {
        Value::Bytes(bytes) => Some(String::from_utf8_lossy(bytes).into_owned()),
        _ => None,
    }
}

/// A file's path inside the torrent, its folders joined with `/`.
fn file_path(file: &Value<'_>) -> Option<String> {
    let Value::Dict(file) = file else {
        return None;
    };
    let parts = file
        .get(b"path.utf-8".as_slice())
        .or(file.get(b"path".as_slice()))?;
    let Value::List(parts) = parts else {
        return None;
    };
    let parts: Vec<String> = parts.iter().map(text).collect::<Option<_>>()?;
    (!parts.is_empty()).then(|| parts.join("/"))
}

/// The files a torrent holds, by path inside it (without the torrent's own folder name). A
/// single-file torrent holds one file, named as the torrent is. `None` when the data is not a
/// torrent with a file list.
pub fn file_list(torrent: &[u8]) -> Option<Vec<String>> {
    let mut at = 0;
    let Value::Dict(root) = value(torrent, &mut at, 0)? else {
        return None;
    };
    let Value::Dict(info) = root.get(b"info".as_slice())? else {
        return None;
    };
    match info.get(b"files".as_slice()) {
        Some(Value::List(files)) => files.iter().map(file_path).collect(),
        Some(_) => None,
        None => {
            let name = info
                .get(b"name.utf-8".as_slice())
                .or(info.get(b"name".as_slice()))?;
            Some(vec![text(name)?])
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn string(text: &str) -> String {
        format!("{}:{text}", text.len())
    }

    fn multi_file(files: &[&[&str]]) -> Vec<u8> {
        let list: String = files
            .iter()
            .map(|path| {
                let parts: String = path.iter().map(|part| string(part)).collect();
                format!("d6:lengthi100e4:pathl{parts}ee")
            })
            .collect();
        format!(
            "d8:announce3:udp4:infod5:filesl{list}e4:name{}12:piece lengthi16384eee",
            string("Show")
        )
        .into_bytes()
    }

    #[test]
    fn the_files_of_a_multi_file_torrent_are_listed_by_their_path() {
        let torrent = multi_file(&[&["[Grp] Show - 01v2.mkv"], &["Extras", "NCOP.mkv"]]);
        assert_eq!(
            file_list(&torrent).unwrap(),
            ["[Grp] Show - 01v2.mkv", "Extras/NCOP.mkv"]
        );
    }

    #[test]
    fn a_single_file_torrent_holds_its_name() {
        let torrent = format!("d4:infod6:lengthi5e4:name{}ee", string("Show - 01.mkv"));
        assert_eq!(file_list(torrent.as_bytes()).unwrap(), ["Show - 01.mkv"]);
    }

    #[test]
    fn data_that_is_no_torrent_has_no_file_list() {
        for data in [
            &b""[..],
            b"not a torrent",
            b"d4:infod5:filesl",
            b"d4:infod5:filesli1eee",
            b"l4:spame",
            b"d4:infod4:name99:shorte",
        ] {
            assert_eq!(file_list(data), None, "{}", String::from_utf8_lossy(data));
        }
        let deep = format!("{}{}", "l".repeat(100), "e".repeat(100));
        assert_eq!(file_list(deep.as_bytes()), None);
    }
}
