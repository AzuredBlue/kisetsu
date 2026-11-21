//! Shrinking an image to a small sample: a whole-number box average first, then a bicubic
//! resampling in fixed point.

/// A raster of 8-bit RGB pixels, three bytes each, row by row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Raster {
    pub width: usize,
    pub height: usize,
    pub rgb: Vec<u8>,
}

impl Raster {
    /// The pixels of rows `top..bottom` as a raster of their own.
    pub fn rows(&self, top: usize, bottom: usize) -> Raster {
        let row_bytes = self.width * 3;
        Raster {
            width: self.width,
            height: bottom - top,
            rgb: self.rgb[top * row_bytes..bottom * row_bytes].to_vec(),
        }
    }

    /// The image shrunk to fit `max` (width, height), keeping its proportions; one already that
    /// small is returned as it is.
    pub fn thumbnail(self, max: (usize, usize)) -> Raster {
        let (max_width, max_height) = max;
        if max_width >= self.width && max_height >= self.height {
            return self;
        }
        let aspect = self.width as f64 / self.height as f64;
        let (width, height) = if max_width as f64 / max_height as f64 >= aspect {
            let height = max_height as f64;
            (
                round_aspect(height * aspect, |n| (aspect - n / height).abs()),
                max_height,
            )
        } else {
            let width = max_width as f64;
            (
                max_width,
                round_aspect(width / aspect, |n| {
                    if n == 0.0 {
                        0.0
                    } else {
                        (aspect - width / n).abs()
                    }
                }),
            )
        };
        if (self.width, self.height) == (width, height) {
            return self;
        }
        self.resized(width, height)
    }

    fn resized(self, width: usize, height: usize) -> Raster {
        let full = (0.0, 0.0, self.width as f64, self.height as f64);
        let factor_x = ((full.2 - full.0) / width as f64 / REDUCING_GAP) as usize;
        let factor_y = ((full.3 - full.1) / height as f64 / REDUCING_GAP) as usize;
        let (factor_x, factor_y) = (factor_x.max(1), factor_y.max(1));
        if factor_x == 1 && factor_y == 1 {
            return resample(&self, (width, height), full);
        }
        // The part of the image the resampling reads, widened by what its filter reaches.
        let reach = BICUBIC_SUPPORT - 0.5;
        let reach_x = reach * (full.2 - full.0) / width as f64;
        let reach_y = reach * (full.3 - full.1) / height as f64;
        let region = (
            ((full.0 - reach_x) as i64).max(0) as usize,
            ((full.1 - reach_y) as i64).max(0) as usize,
            ((full.2 + reach_x).ceil() as usize).min(self.width),
            ((full.3 + reach_y).ceil() as usize).min(self.height),
        );
        let reduced = self.reduced((factor_x, factor_y), region);
        let (left, top) = (region.0 as f64, region.1 as f64);
        let (factor_x, factor_y) = (factor_x as f64, factor_y as f64);
        let sub = (
            (full.0 - left) / factor_x,
            (full.1 - top) / factor_y,
            (full.2 - left) / factor_x,
            (full.3 - top) / factor_y,
        );
        resample(&reduced, (width, height), sub)
    }

    /// Each `factor`-sized block of `region` (left, top, right, bottom) averaged into one pixel; a
    /// block cut short by the region's edge averages what it has.
    fn reduced(&self, factor: (usize, usize), region: (usize, usize, usize, usize)) -> Raster {
        let (left, top, right, bottom) = region;
        let width = (right - left).div_ceil(factor.0);
        let height = (bottom - top).div_ceil(factor.1);
        let mut rgb = Vec::with_capacity(width * height * 3);
        for block_y in 0..height {
            let from_y = top + block_y * factor.1;
            let to_y = bottom.min(from_y + factor.1);
            for block_x in 0..width {
                let from_x = left + block_x * factor.0;
                let to_x = right.min(from_x + factor.0);
                let pixels = ((to_x - from_x) * (to_y - from_y)) as u32;
                let multiplier = (1u32 << 24) / pixels;
                let amend = pixels / 2;
                let mut sums = [0u32; 3];
                for y in from_y..to_y {
                    let start = (y * self.width + from_x) * 3;
                    for pixel in self.rgb[start..start + (to_x - from_x) * 3].chunks_exact(3) {
                        for (sum, &value) in sums.iter_mut().zip(pixel) {
                            *sum += u32::from(value);
                        }
                    }
                }
                for sum in sums {
                    rgb.push(((u64::from(sum + amend) * u64::from(multiplier)) >> 24) as u8);
                }
            }
        }
        Raster { width, height, rgb }
    }
}

/// The width or height nearest the exact `number` that keeps the proportions best.
fn round_aspect(number: f64, error: impl Fn(f64) -> f64) -> usize {
    let (below, above) = (number.floor(), number.ceil());
    let nearest = if error(below) <= error(above) {
        below
    } else {
        above
    };
    (nearest as usize).max(1)
}

/// Boxes averaged first leave the resampling at least this many times the final size to cover.
const REDUCING_GAP: f64 = 2.0;
const BICUBIC_SUPPORT: f64 = 2.0;
const PRECISION_BITS: u32 = 22;

/// The filter weights of every output position along one axis.
struct Weights {
    /// First input position and number of inputs of each output position.
    bounds: Vec<(usize, usize)>,
    /// `kernel` weights per output position, in fixed point.
    kernel: usize,
    fixed: Vec<i32>,
}

fn bicubic(distance: f64) -> f64 {
    const A: f64 = -0.5;
    let x = distance.abs();
    if x < 1.0 {
        ((A + 2.0) * x - (A + 3.0)) * x * x + 1.0
    } else if x < 2.0 {
        (((x - 5.0) * x + 8.0) * x - 4.0) * A
    } else {
        0.0
    }
}

/// The weights that resample the span `from..to` of an axis of `size` inputs into `out` outputs.
fn weights(size: usize, from: f64, to: f64, out: usize) -> Weights {
    let (from, to) = (f64::from(from as f32), f64::from(to as f32));
    let span = f64::from((to - from) as f32);
    let scale = span / out as f64;
    let filter_scale = scale.max(1.0);
    let support = BICUBIC_SUPPORT * filter_scale;
    let kernel = support.ceil() as usize * 2 + 1;
    let mut bounds = Vec::with_capacity(out);
    let mut fixed = vec![0i32; out * kernel];
    let unit = f64::from(1u32 << PRECISION_BITS);
    for position in 0..out {
        let center = from + (position as f64 + 0.5) * scale;
        let inverse = 1.0 / filter_scale;
        let first = ((center - support + 0.5) as i64).max(0) as usize;
        let last = (((center + support + 0.5) as i64).max(0) as usize).min(size);
        let count = last.saturating_sub(first);
        let mut weight: Vec<f64> = (0..count)
            .map(|x| bicubic(((x + first) as f64 - center + 0.5) * inverse))
            .collect();
        let mut total = 0.0;
        for value in &weight {
            total += value;
        }
        if total != 0.0 {
            for value in &mut weight {
                *value /= total;
            }
        }
        for (slot, value) in fixed[position * kernel..].iter_mut().zip(&weight) {
            *slot = if *value < 0.0 {
                (-0.5 + value * unit) as i32
            } else {
                (0.5 + value * unit) as i32
            };
        }
        bounds.push((first, count));
    }
    Weights {
        bounds,
        kernel,
        fixed,
    }
}

fn clip(sum: i64) -> u8 {
    (sum >> PRECISION_BITS).clamp(0, 255) as u8
}

/// `source` resampled to `size` (width, height) from the span `span` (left, top, right, bottom).
fn resample(source: &Raster, size: (usize, usize), span: (f64, f64, f64, f64)) -> Raster {
    let (width, height) = size;
    let horizontal = width != source.width || span.0 != 0.0 || span.2 != width as f64;
    let vertical = height != source.height || span.1 != 0.0 || span.3 != height as f64;
    let vertical_first = horizontal
        && vertical
        && (source.height as i64 - height as i64) > 2 * (source.width as i64 - width as i64);
    let across = || weights(source.width, span.0, span.2, width);
    let down = || weights(source.height, span.1, span.3, height);
    match (horizontal, vertical, vertical_first) {
        (true, true, true) => {
            let tall = resample_rows(source, &down(), height);
            resample_columns(&tall, &across(), width)
        }
        (true, true, false) => {
            let wide = resample_columns(source, &across(), width);
            resample_rows(&wide, &down(), height)
        }
        (true, false, _) => resample_columns(source, &across(), width),
        (false, true, _) => resample_rows(source, &down(), height),
        (false, false, _) => source.clone(),
    }
}

/// Resamples along each row: the output is `width` pixels wide.
fn resample_columns(source: &Raster, weights: &Weights, width: usize) -> Raster {
    let mut rgb = vec![0u8; width * source.height * 3];
    for y in 0..source.height {
        let row = &source.rgb[y * source.width * 3..(y + 1) * source.width * 3];
        for (x, &(first, count)) in weights.bounds.iter().enumerate() {
            let kernel = &weights.fixed[x * weights.kernel..x * weights.kernel + count];
            for channel in 0..3 {
                let mut sum = 1i64 << (PRECISION_BITS - 1);
                for (offset, &weight) in kernel.iter().enumerate() {
                    sum += i64::from(row[(first + offset) * 3 + channel]) * i64::from(weight);
                }
                rgb[(y * width + x) * 3 + channel] = clip(sum);
            }
        }
    }
    Raster {
        width,
        height: source.height,
        rgb,
    }
}

/// Resamples along each column: the output is `height` pixels tall.
fn resample_rows(source: &Raster, weights: &Weights, height: usize) -> Raster {
    let mut rgb = vec![0u8; source.width * height * 3];
    for (y, &(first, count)) in weights.bounds.iter().enumerate() {
        let kernel = &weights.fixed[y * weights.kernel..y * weights.kernel + count];
        for x in 0..source.width {
            for channel in 0..3 {
                let mut sum = 1i64 << (PRECISION_BITS - 1);
                for (offset, &weight) in kernel.iter().enumerate() {
                    let at = ((first + offset) * source.width + x) * 3 + channel;
                    sum += i64::from(source.rgb[at]) * i64::from(weight);
                }
                rgb[(y * source.width + x) * 3 + channel] = clip(sum);
            }
        }
    }
    Raster {
        width: source.width,
        height,
        rgb,
    }
}
