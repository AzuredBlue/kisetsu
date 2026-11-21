//! Reducing an image to a few representative colours by median cut: the colours are split into
//! boxes, the box holding the most pixels first, each at the median of its widest axis.

use std::collections::HashMap;

/// How much each channel's range counts when choosing the axis to split (luma weights).
const AXIS_WEIGHTS: [i64; 3] = [77, 150, 29];

#[derive(Debug, Clone, Copy)]
struct Colour {
    rgb: [u8; 3],
    count: u32,
}

#[derive(Debug)]
struct Node {
    colours: Vec<Colour>,
    /// The boxes this one was split into: the lower half, then the upper.
    halves: Option<(usize, usize)>,
}

/// A max-heap of boxes by pixel count, with a fixed order among boxes of equal count.
#[derive(Debug, Default)]
struct Heap {
    /// 1-based: (pixel count, node).
    items: Vec<(u32, usize)>,
}

impl Heap {
    fn new() -> Self {
        Self {
            items: vec![(0, 0)],
        }
    }

    fn is_empty(&self) -> bool {
        self.items.len() == 1
    }

    fn push(&mut self, item: (u32, usize)) {
        self.items.push(item);
        let mut at = self.items.len() - 1;
        while at > 1 && self.items[at / 2].0 < self.items[at].0 {
            self.items.swap(at / 2, at);
            at /= 2;
        }
    }

    fn pop(&mut self) -> Option<(u32, usize)> {
        if self.is_empty() {
            return None;
        }
        let last = self.items.pop()?;
        if self.is_empty() {
            return Some(last);
        }
        let top = std::mem::replace(&mut self.items[1], last);
        let end = self.items.len() - 1;
        let mut at = 1;
        while at * 2 <= end {
            let mut child = at * 2;
            if child < end && self.items[child + 1].0 > self.items[child].0 {
                child += 1;
            }
            if self.items[child].0 < self.items[at].0 {
                break;
            }
            self.items.swap(child, at);
            at = child;
        }
        Some(top)
    }
}

fn count_of(colours: &[Colour]) -> u32 {
    colours.iter().map(|colour| colour.count).sum()
}

/// The axis whose weighted range is widest (the first on a tie), or `None` when all colours are
/// the same.
fn widest_axis(colours: &[Colour]) -> Option<usize> {
    let mut low = [u8::MAX; 3];
    let mut high = [u8::MIN; 3];
    for colour in colours {
        for axis in 0..3 {
            low[axis] = low[axis].min(colour.rgb[axis]);
            high[axis] = high[axis].max(colour.rgb[axis]);
        }
    }
    let mut widest = None;
    let mut widest_range = 0;
    for axis in 0..3 {
        let range = i64::from(high[axis] - low[axis]) * AXIS_WEIGHTS[axis];
        if range > widest_range {
            widest = Some(axis);
            widest_range = range;
        }
    }
    widest
}

/// Splits a box in two at the colour value where half its pixels have been passed, along `axis`:
/// colours below that value go to the lower half, the rest to the upper.
fn split(mut colours: Vec<Colour>, axis: usize) -> (Vec<Colour>, Vec<Colour>) {
    colours.sort_by_key(|colour| colour.rgb[axis]);
    let mut left = count_of(&colours).div_ceil(2);
    let mut median = colours[0].rgb[axis];
    for colour in &colours {
        median = colour.rgb[axis];
        if left > colour.count {
            left -= colour.count;
        } else {
            break;
        }
    }
    let (mut lower, mut upper): (Vec<Colour>, Vec<Colour>) =
        colours.iter().partition(|colour| colour.rgb[axis] < median);
    if lower.is_empty() {
        (lower, upper) = colours
            .iter()
            .partition(|colour| colour.rgb[axis] <= median);
    }
    (lower, upper)
}

/// A colour of the reduced palette and how many pixels were given it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Swatch {
    pub rgb: [u8; 3],
    pub pixels: u32,
}

/// The pixels reduced to at most `colours` colours, in palette order, leaving out a colour no
/// pixel is nearest to.
pub fn quantize(rgb: &[u8], colours: usize) -> Vec<Swatch> {
    let mut counts: HashMap<[u8; 3], u32> = HashMap::new();
    for pixel in rgb.chunks_exact(3) {
        *counts.entry([pixel[0], pixel[1], pixel[2]]).or_default() += 1;
    }
    let mut unique: Vec<Colour> = counts
        .into_iter()
        .map(|(rgb, count)| Colour { rgb, count })
        .collect();
    unique.sort_by_key(|colour| colour.rgb);
    if unique.is_empty() {
        return Vec::new();
    }
    let mut nodes = vec![Node {
        colours: unique,
        halves: None,
    }];
    let mut heap = Heap::new();
    heap.push((count_of(&nodes[0].colours), 0));
    let mut boxes = 1;
    while boxes < colours {
        let Some((_, at)) = heap.pop() else { break };
        let Some(axis) = widest_axis(&nodes[at].colours) else {
            continue;
        };
        let (lower, upper) = split(std::mem::take(&mut nodes[at].colours), axis);
        let (lower_count, upper_count) = (count_of(&lower), count_of(&upper));
        let first = nodes.len();
        nodes.push(Node {
            colours: lower,
            halves: None,
        });
        nodes.push(Node {
            colours: upper,
            halves: None,
        });
        nodes[at].halves = Some((first, first + 1));
        heap.push((upper_count, first + 1));
        heap.push((lower_count, first));
        boxes += 1;
    }

    // The boxes from the highest to the lowest, each box's upper half before its lower.
    let mut leaves = Vec::new();
    let mut pending = vec![0usize];
    while let Some(at) = pending.pop() {
        match nodes[at].halves {
            Some((lower, upper)) => {
                pending.push(lower);
                pending.push(upper);
            }
            None => leaves.push(at),
        }
    }
    let palette: Vec<[u8; 3]> = leaves
        .iter()
        .map(|&leaf| {
            let colours = &nodes[leaf].colours;
            let total = u64::from(count_of(colours));
            let mut average = [0u8; 3];
            for (axis, slot) in average.iter_mut().enumerate() {
                let sum: u64 = colours
                    .iter()
                    .map(|colour| u64::from(colour.rgb[axis]) * u64::from(colour.count))
                    .sum();
                *slot = ((2 * sum + total) / (2 * total)) as u8;
            }
            average
        })
        .collect();

    let distance = |a: [u8; 3], b: [u8; 3]| -> i32 {
        a.iter()
            .zip(b)
            .map(|(&x, y)| (i32::from(x) - i32::from(y)).pow(2))
            .sum()
    };
    let mut swatches: Vec<Swatch> = palette
        .iter()
        .map(|&rgb| Swatch { rgb, pixels: 0 })
        .collect();
    for (own, &leaf) in leaves.iter().enumerate() {
        for colour in &nodes[leaf].colours {
            // The nearest palette colour; on a tie the box the colour came from, else the entry
            // nearest that box.
            let nearest = (0..palette.len())
                .min_by_key(|&other| {
                    (
                        distance(colour.rgb, palette[other]),
                        other != own,
                        distance(palette[own], palette[other]),
                        other,
                    )
                })
                .unwrap_or(own);
            swatches[nearest].pixels += colour.count;
        }
    }
    swatches.retain(|swatch| swatch.pixels > 0);
    swatches
}
