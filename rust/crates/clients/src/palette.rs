//! Representative colours of a show's banner or cover, used to colour its page and its poster card.

pub mod quantize;
pub mod thumbnail;

use std::io::Cursor;
use std::time::Duration;

use image::ImageReader;
use thiserror::Error;

use crate::traffic::{self, Outcome};
use crate::url;

pub use thumbnail::Raster;

/// The most bytes of an image that are read.
pub const MAX_IMAGE_BYTES: u64 = 5 * 1024 * 1024;
/// The size an image is shrunk to before its colours are read.
const SAMPLE_SIZE: (usize, usize) = (96, 54);
const CLUSTERS: usize = 16;
const MIN_COLOURFULNESS: f64 = 0.03;
const MIN_ACCENT_CHROMA: f64 = 0.08;
/// A dark or grey image still has a colour to give when some of it is this vivid, such as neon
/// lines on black.
const VIVID_ACCENT_CHROMA: f64 = 0.3;
const MIN_TINT_CHROMA: f64 = 0.04;
const FAMILIES: usize = 12;
const SECONDARY_MIN_DISTANCE: f64 = 60.0;
const SECONDARY_MIN_SCORE: f64 = 0.15;
/// Drawn skin is a light orange: these hues at this lightness or above. Pinks and salmons, and
/// anything darker, are clothes, hair or light, and count in full.
const SKIN_HUES: (f64, f64) = (18.0, 40.0);
const SKIN_MIN_LIGHTNESS: f64 = 0.75;
const SKIN_WEIGHT: f64 = 0.4;
/// The part of an image (top and bottom, as fractions of its height) at the foot of the show
/// page's banner, where the banner fades into the page: a banner's lower edge, or the middle of a
/// poster, which the page crops to a wide band when it stands in for a banner.
pub const BANNER_FADE: Fade = Fade(0.65, 1.0);
pub const COVER_FADE: Fade = Fade(0.35, 0.65);
/// The most pixels along one side that an image may have.
const MAX_SIDE: u32 = 16_384;
const MAX_DECODE_BYTES: u64 = 128 * 1024 * 1024;

/// A band of an image, by the fractions of its height where it starts and ends.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Fade(pub f64, pub f64);

/// The colours that stand for an image.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Hues {
    pub accent_hue: u16,
    pub accent_saturation: f64,
    pub secondary_hue: u16,
    pub tint_hue: u16,
    pub tint_saturation: f64,
}

/// An image could not be read.
#[derive(Debug, Error)]
pub enum ImageError {
    #[error("{0} is not an AniList image address")]
    NotAllowed(String),
    #[error("{host} did not answer: {detail}")]
    Unreachable { host: String, detail: String },
    #[error("{host} answered HTTP {status}")]
    Status { host: String, status: u16 },
    #[error("the image is larger than {} MB", MAX_IMAGE_BYTES / (1024 * 1024))]
    TooLarge,
    #[error("the image cannot be decoded: {0}")]
    Decode(String),
}

/// Whether an image may be fetched from this address: HTTPS on anilist.co or a subdomain.
pub fn is_allowed_image_url(address: &str) -> bool {
    let parts = url::split(address);
    let host = url::hostname(address).unwrap_or_default();
    let allowed = parts.scheme.eq_ignore_ascii_case("https")
        && (host == "anilist.co" || host.ends_with(".anilist.co"));
    // The connection must go where the check looked: both readings of the address name one host.
    let connects_to = address
        .parse::<ureq::http::Uri>()
        .ok()
        .and_then(|uri| uri.host().map(str::to_lowercase));
    allowed && connects_to.as_deref() == Some(host.as_str())
}

/// Where images come from.
pub trait ImageSource: Send + Sync {
    /// The bytes of the image at `address`, counted in the request counts under `kind`.
    fn get(&self, address: &str, kind: &str) -> Result<Vec<u8>, ImageError>;
}

/// Images from the web, only from AniList.
pub struct WebImages {
    agent: ureq::Agent,
}

impl Default for WebImages {
    fn default() -> Self {
        let timeout = Duration::from_secs(10);
        let agent = ureq::Agent::config_builder()
            .http_status_as_error(false)
            // Redirects stay off: the host allow-list only covers the address asked for.
            .max_redirects(0)
            .max_redirects_will_error(false)
            // Requests are minutes apart, and an idle connection would keep its buffers and TLS
            // state until the next one.
            .max_idle_connections(0)
            .timeout_connect(Some(timeout))
            .timeout_per_read(Some(timeout))
            .build()
            .new_agent();
        Self { agent }
    }
}

impl ImageSource for WebImages {
    fn get(&self, address: &str, kind: &str) -> Result<Vec<u8>, ImageError> {
        if !is_allowed_image_url(address) {
            return Err(ImageError::NotAllowed(address.to_owned()));
        }
        self.download(address, kind)
    }
}

impl WebImages {
    fn download(&self, address: &str, kind: &str) -> Result<Vec<u8>, ImageError> {
        let host = traffic::host_of(address);
        let response = match self.agent.get(address).call() {
            Ok(response) => response,
            Err(error) => {
                traffic::record(kind, &host, Outcome::Error);
                return Err(ImageError::Unreachable {
                    host,
                    detail: error.to_string(),
                });
            }
        };
        let status = response.status().as_u16();
        traffic::record(kind, &host, Outcome::of_status(status));
        if status >= 300 {
            return Err(ImageError::Status { host, status });
        }
        match response
            .into_body()
            .with_config()
            .limit(MAX_IMAGE_BYTES)
            .read_to_vec()
        {
            Ok(bytes) => Ok(bytes),
            Err(ureq::Error::BodyExceedsLimit(_)) => Err(ImageError::TooLarge),
            Err(error) => Err(ImageError::Unreachable {
                host,
                detail: error.to_string(),
            }),
        }
    }
}

// -- reading the colours --

/// The traffic kind of a show's colours.
const COVER_ART: &str = "Cover art";

/// Reads a show's colours from its banner, or from its cover when the banner has none.
///
/// `Err` means an image could not be fetched or decoded and nothing was found, so the caller can
/// try again later instead of remembering a failure; `Ok(None)` means the images have no colour.
pub fn fetch_hues(
    images: &dyn ImageSource,
    banner: &str,
    cover: &str,
) -> Result<Option<Hues>, ImageError> {
    let mut failure = None;
    for (address, fade) in [(banner, BANNER_FADE), (cover, COVER_FADE)] {
        if address.is_empty() {
            continue;
        }
        let read = images
            .get(address, COVER_ART)
            .and_then(|bytes| dominant_hues(&bytes, fade));
        match read {
            Ok(Some(hues)) => return Ok(Some(hues)),
            Ok(None) => {}
            Err(error) => {
                tracing::debug!("Could not read colours from {address}: {error}");
                failure.get_or_insert(error);
            }
        }
    }
    failure.map_or(Ok(None), Err)
}

/// Decodes an image into 8-bit RGB pixels.
pub fn decode(bytes: &[u8]) -> Result<Raster, ImageError> {
    let mut reader = ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .map_err(|error| ImageError::Decode(error.to_string()))?;
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(MAX_SIDE);
    limits.max_image_height = Some(MAX_SIDE);
    limits.max_alloc = Some(MAX_DECODE_BYTES);
    reader.limits(limits);
    let decoded = reader
        .decode()
        .map_err(|error| ImageError::Decode(error.to_string()))?
        .into_rgb8();
    Ok(Raster {
        width: decoded.width() as usize,
        height: decoded.height() as usize,
        rgb: decoded.into_raw(),
    })
}

/// The colours that represent an image, or `None` when it has no real colour.
///
/// The accent comes from the hue family that stands out most in the whole image (how much of it
/// the family covers, and how vivid it is, with skin counting for less). The tint comes from the
/// family that covers most of the `fade` band, the part of the image the page's own colour takes
/// over from, so the page carries on from it.
pub fn dominant_hues(bytes: &[u8], fade: Fade) -> Result<Option<Hues>, ImageError> {
    Ok(hues_of(decode(bytes)?, fade))
}

/// [`dominant_hues`] of pixels already decoded.
pub fn hues_of(image: Raster, fade: Fade) -> Option<Hues> {
    let top = (image.height as f64 * fade.0) as usize;
    let bottom = top + 1;
    let bottom = bottom.max((image.height as f64 * fade.1) as usize);
    let band = image.rows(top, bottom).thumbnail(SAMPLE_SIZE);
    let sample = image.thumbnail(SAMPLE_SIZE);
    let (colourfulness, families) = families_of(&sample);
    let (_, band_families) = families_of(&band);

    let accent = first_greatest(&families, |family| family.score)?;
    if accent.chroma < MIN_ACCENT_CHROMA
        || (colourfulness < MIN_COLOURFULNESS && accent.chroma < VIVID_ACCENT_CHROMA)
    {
        return None;
    }
    let tinted: Vec<Family> = band_families
        .iter()
        .filter(|family| family.chroma >= MIN_TINT_CHROMA && !family.skin)
        .copied()
        .collect();
    let tint = first_greatest(&tinted, |family| family.share).unwrap_or(accent);
    let mut secondary: Option<Family> = None;
    for family in &families {
        let apart = (family.hue - accent.hue).abs();
        let apart = apart.min(360.0 - apart);
        if apart >= SECONDARY_MIN_DISTANCE
            && family.score >= accent.score * SECONDARY_MIN_SCORE
            && secondary.is_none_or(|best| family.score > best.score)
        {
            secondary = Some(*family);
        }
    }
    let secondary_hue = secondary.map_or_else(|| (accent.hue + 35.0).rem_euclid(360.0), |f| f.hue);
    Some(Hues {
        accent_hue: whole_degrees(accent.hue),
        accent_saturation: accent.saturation,
        secondary_hue: whole_degrees(secondary_hue),
        tint_hue: whole_degrees(tint.hue),
        tint_saturation: tint.saturation,
    })
}

fn whole_degrees(hue: f64) -> u16 {
    (hue.round_ties_even() as i64).rem_euclid(360) as u16
}

/// The first of `items` with the greatest `key`.
fn first_greatest(items: &[Family], key: impl Fn(&Family) -> f64) -> Option<Family> {
    let mut best: Option<Family> = None;
    for item in items {
        if best.is_none_or(|current| key(item) > key(&current)) {
            best = Some(*item);
        }
    }
    best
}

/// The colours of one hue, merged.
#[derive(Debug, Clone, Copy)]
struct Family {
    share: f64,
    hue: f64,
    saturation: f64,
    chroma: f64,
    skin: bool,
    score: f64,
}

/// Hue (a fraction of a turn), lightness and saturation of an RGB colour in `0..=1`.
fn hls(r: f64, g: f64, b: f64) -> (f64, f64, f64) {
    let max = r.max(g).max(b);
    let min = r.min(g).min(b);
    let sum = max + min;
    let range = max - min;
    let lightness = sum / 2.0;
    if min == max {
        return (0.0, lightness, 0.0);
    }
    let saturation = if lightness <= 0.5 {
        range / sum
    } else {
        range / (2.0 - max - min)
    };
    let (rc, gc, bc) = ((max - r) / range, (max - g) / range, (max - b) / range);
    let hue = if r == max {
        bc - gc
    } else if g == max {
        2.0 + rc - bc
    } else {
        4.0 + gc - rc
    };
    ((hue / 6.0).rem_euclid(1.0), lightness, saturation)
}

/// The image's hue families and how colourful the image is overall.
///
/// The image is reduced to a handful of colour clusters, which are grouped by hue; near-black and
/// near-white clusters belong to no family.
fn families_of(image: &Raster) -> (f64, Vec<Family>) {
    let swatches = quantize::quantize(&image.rgb, CLUSTERS);
    let total: u32 = swatches.iter().map(|swatch| swatch.pixels).sum();
    if total == 0 {
        return (0.0, Vec::new());
    }
    // Per family, in order of first appearance: share, then share-weighted r, g, b and chroma.
    let mut sums: Vec<(usize, [f64; 5])> = Vec::new();
    let mut colourfulness = 0.0;
    for swatch in &swatches {
        let [r, g, b] = swatch.rgb.map(f64::from);
        let (hue, lightness, saturation) = hls(r / 255.0, g / 255.0, b / 255.0);
        let share = f64::from(swatch.pixels) / f64::from(total);
        let chroma = saturation * (1.0 - (2.0 * lightness - 1.0).abs());
        colourfulness += share * chroma;
        if lightness < 0.10 || lightness > 0.93 {
            continue;
        }
        let key = (hue * FAMILIES as f64) as usize % FAMILIES;
        let at = match sums.iter().position(|(existing, _)| *existing == key) {
            Some(at) => at,
            None => {
                sums.push((key, [0.0; 5]));
                sums.len() - 1
            }
        };
        let family = &mut sums[at].1;
        family[0] += share;
        family[1] += share * r;
        family[2] += share * g;
        family[3] += share * b;
        family[4] += share * chroma;
    }
    let families = sums
        .into_iter()
        .map(|(_, [share, r, g, b, chroma_sum])| {
            let (hue, lightness, saturation) =
                hls(r / share / 255.0, g / share / 255.0, b / share / 255.0);
            let degrees = hue * 360.0;
            let skin =
                (SKIN_HUES.0..=SKIN_HUES.1).contains(&degrees) && lightness >= SKIN_MIN_LIGHTNESS;
            let chroma = chroma_sum / share;
            let weight = if skin { SKIN_WEIGHT } else { 1.0 };
            Family {
                share,
                hue: degrees,
                saturation,
                chroma,
                skin,
                score: share.powf(0.6) * (chroma + 0.04) * weight,
            }
        })
        .collect();
    (colourfulness, families)
}

#[cfg(test)]
mod tests {
    use std::io::{Read, Write};
    use std::net::TcpListener;

    use super::*;

    /// Answers each connection with the head, then `length` bytes of body.
    fn serve(head: &'static str, length: usize) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = format!("http://{}/image", listener.local_addr().unwrap());
        std::thread::spawn(move || {
            for mut stream in listener.incoming().flatten() {
                let mut request = [0u8; 2048];
                let _ = stream.read(&mut request);
                let _ = stream.write_all(
                    format!(
                        "{head}Content-Length: {length}
Connection: close

"
                    )
                    .as_bytes(),
                );
                let _ = stream.write_all(&vec![7u8; length]);
            }
        });
        address
    }

    #[test]
    fn an_image_up_to_the_limit_is_read_and_a_larger_one_is_refused() {
        let images = WebImages::default();
        let at_limit = serve(
            "HTTP/1.1 200 OK
",
            MAX_IMAGE_BYTES as usize,
        );
        assert_eq!(
            images.download(&at_limit, "test").unwrap().len(),
            MAX_IMAGE_BYTES as usize
        );
        let over = serve(
            "HTTP/1.1 200 OK
",
            MAX_IMAGE_BYTES as usize + 1,
        );
        assert!(matches!(
            images.download(&over, "test"),
            Err(ImageError::TooLarge)
        ));
    }

    #[test]
    fn an_error_status_and_a_redirect_are_failures() {
        let images = WebImages::default();
        let missing = serve(
            "HTTP/1.1 404 Not Found
",
            0,
        );
        assert!(matches!(
            images.download(&missing, "test"),
            Err(ImageError::Status { status: 404, .. })
        ));
        let moved = serve(
            "HTTP/1.1 302 Found
Location: https://evil.example/x
",
            0,
        );
        assert!(matches!(
            images.download(&moved, "test"),
            Err(ImageError::Status { status: 302, .. })
        ));
    }

    #[test]
    fn only_https_anilist_addresses_are_fetched() {
        assert!(matches!(
            WebImages::default().get("http://127.0.0.1:1/x.png", "test"),
            Err(ImageError::NotAllowed(_))
        ));
    }

    struct Scripted(Vec<Result<Vec<u8>, ()>>, std::sync::Mutex<usize>);

    impl ImageSource for Scripted {
        fn get(&self, _address: &str, _kind: &str) -> Result<Vec<u8>, ImageError> {
            let mut next = self.1.lock().unwrap();
            let step = self.0.get(*next).cloned().unwrap_or(Err(()));
            *next += 1;
            step.map_err(|()| ImageError::TooLarge)
        }
    }

    fn red_png() -> Vec<u8> {
        let mut bytes = Vec::new();
        let pixels = image::RgbImage::from_pixel(8, 8, image::Rgb([220, 20, 20]));
        pixels
            .write_to(&mut Cursor::new(&mut bytes), image::ImageFormat::Png)
            .unwrap();
        bytes
    }

    #[test]
    fn the_cover_is_read_when_the_banner_has_no_colour_and_a_failure_counts_only_when_nothing_is_found()
     {
        let grey = {
            let mut bytes = Vec::new();
            image::RgbImage::from_pixel(8, 8, image::Rgb([128, 128, 128]))
                .write_to(&mut Cursor::new(&mut bytes), image::ImageFormat::Png)
                .unwrap();
            bytes
        };
        let source = Scripted(vec![Ok(grey.clone()), Ok(red_png())], Default::default());
        assert!(
            fetch_hues(
                &source,
                "https://s4.anilist.co/b",
                "https://s4.anilist.co/c"
            )
            .unwrap()
            .is_some()
        );
        let source = Scripted(vec![Err(()), Ok(red_png())], Default::default());
        assert!(fetch_hues(&source, "b", "c").unwrap().is_some());
        let source = Scripted(vec![Err(()), Ok(grey.clone())], Default::default());
        assert!(fetch_hues(&source, "b", "c").is_err());
        let source = Scripted(vec![Ok(grey.clone()), Ok(grey)], Default::default());
        assert_eq!(fetch_hues(&source, "b", "c").unwrap(), None);
        let source = Scripted(Vec::new(), Default::default());
        assert_eq!(fetch_hues(&source, "", "").unwrap(), None);
        let source = Scripted(vec![Ok(b"not an image".to_vec())], Default::default());
        assert!(fetch_hues(&source, "b", "").is_err());
    }
}
