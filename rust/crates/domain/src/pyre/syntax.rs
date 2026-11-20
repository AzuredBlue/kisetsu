//! Reads Python `re` pattern syntax into a tree.
//!
//! It reads the syntax Kisetsu's patterns use. Python syntax beyond that (backreferences, `\B`,
//! inline flags, conditionals, atomic groups, possessive repeats, comments, `\N{...}`, octal
//! escapes) is an error, as is everything Python itself rejects.

use super::Error;

pub(super) enum Node {
    Literal(char),
    Set(Set),
    /// `^` and `\A`: the start of the text.
    Start,
    /// `$`: the end of the text, or before a newline that ends it.
    End,
    /// `\Z`: the end of the text.
    EndOfText,
    /// `\b`.
    WordBoundary,
    /// A capturing group, named or not.
    Capture(Option<String>, Box<Node>),
    /// A non-capturing group.
    Group(Box<Node>),
    Look {
        behind: bool,
        negated: bool,
        node: Box<Node>,
    },
    Repeat {
        node: Box<Node>,
        min: u32,
        max: Option<u32>,
        greedy: bool,
    },
    Concat(Vec<Node>),
    Alternation(Vec<Node>),
}

/// A character set: `[...]`, `.`, or a category escape outside brackets.
pub(super) struct Set {
    pub negated: bool,
    pub items: Vec<Item>,
}

pub(super) enum Item {
    Range(char, char),
    Category(Category, bool),
}

#[derive(Clone, Copy)]
pub(super) enum Category {
    Digit,
    Space,
    Word,
}

/// Python's limit on repeat counts.
const MAX_REPEAT: u64 = u32::MAX as u64;

pub(super) fn parse(pattern: &str) -> Result<Node, Error> {
    let mut parser = Parser {
        chars: pattern.chars().collect(),
        pos: 0,
        names: Vec::new(),
    };
    let node = parser.alternation()?;
    if parser.pos < parser.chars.len() {
        return Err(parser.error("unbalanced parenthesis"));
    }
    Ok(node)
}

struct Parser {
    chars: Vec<char>,
    pos: usize,
    names: Vec<String>,
}

impl Parser {
    fn error(&self, message: &str) -> Error {
        Error(format!("{message} at position {}", self.pos))
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.pos).copied()
    }

    fn next(&mut self) -> Option<char> {
        let c = self.peek()?;
        self.pos += 1;
        Some(c)
    }

    fn eat(&mut self, c: char) -> bool {
        let found = self.peek() == Some(c);
        if found {
            self.pos += 1;
        }
        found
    }

    fn alternation(&mut self) -> Result<Node, Error> {
        let mut branches = vec![self.concat()?];
        while self.eat('|') {
            branches.push(self.concat()?);
        }
        Ok(if branches.len() == 1 {
            branches.remove(0)
        } else {
            Node::Alternation(branches)
        })
    }

    fn concat(&mut self) -> Result<Node, Error> {
        let mut items = Vec::new();
        while let Some(c) = self.peek() {
            if c == '|' || c == ')' {
                break;
            }
            self.pos += 1;
            let item = match c {
                '\\' => self.escape()?,
                '[' => Node::Set(self.set()?),
                '.' => Node::Set(Set {
                    negated: true,
                    items: vec![Item::Range('\n', '\n')],
                }),
                '^' => Node::Start,
                '$' => Node::End,
                '(' => self.group()?,
                '*' | '+' | '?' | '{' => {
                    let Some((min, max)) = self.repeat_bounds(c)? else {
                        items.push(Node::Literal('{'));
                        continue;
                    };
                    let node = match items.pop() {
                        None
                        | Some(Node::Start | Node::End | Node::EndOfText | Node::WordBoundary) => {
                            return Err(self.error("nothing to repeat"));
                        }
                        Some(Node::Repeat { .. }) => return Err(self.error("multiple repeat")),
                        Some(node) => node,
                    };
                    let greedy = !self.eat('?');
                    if self.peek() == Some('+') {
                        return Err(self.error("possessive repeats are not supported"));
                    }
                    Node::Repeat {
                        node: Box::new(node),
                        min,
                        max,
                        greedy,
                    }
                }
                c => Node::Literal(c),
            };
            items.push(item);
        }
        Ok(Node::Concat(items))
    }

    /// The bounds of a repeat, or None for a `{` that is a literal because no valid `{m,n}` follows.
    fn repeat_bounds(&mut self, c: char) -> Result<Option<(u32, Option<u32>)>, Error> {
        match c {
            '*' => return Ok(Some((0, None))),
            '+' => return Ok(Some((1, None))),
            '?' => return Ok(Some((0, Some(1)))),
            _ => {}
        }
        let start = self.pos;
        if self.peek() == Some('}') {
            return Ok(None);
        }
        let low = self.ascii_digits();
        let high = if self.eat(',') {
            self.ascii_digits()
        } else {
            low.clone()
        };
        if !self.eat('}') {
            self.pos = start;
            return Ok(None);
        }
        let min = match low.as_str() {
            "" => 0,
            text => self.repeat_count(text)?,
        };
        let max = match high.as_str() {
            "" => None,
            text => Some(self.repeat_count(text)?),
        };
        if max.is_some_and(|max| max < min) {
            return Err(self.error("min repeat greater than max repeat"));
        }
        Ok(Some((min, max)))
    }

    fn ascii_digits(&mut self) -> String {
        let mut digits = String::new();
        while let Some(c) = self.peek().filter(char::is_ascii_digit) {
            digits.push(c);
            self.pos += 1;
        }
        digits
    }

    fn repeat_count(&self, digits: &str) -> Result<u32, Error> {
        digits
            .parse::<u64>()
            .ok()
            .filter(|&count| count < MAX_REPEAT)
            .and_then(|count| u32::try_from(count).ok())
            .ok_or_else(|| self.error("the repetition number is too large"))
    }

    fn group(&mut self) -> Result<Node, Error> {
        if !self.eat('?') {
            return self.capture(None);
        }
        match self.next() {
            Some(':') => {
                let node = self.alternation()?;
                self.close()?;
                Ok(Node::Group(Box::new(node)))
            }
            Some('P') if self.eat('<') => {
                let mut name = String::new();
                loop {
                    match self.next() {
                        Some('>') => break,
                        Some(c) => name.push(c),
                        None => return Err(self.error("missing >, unterminated name")),
                    }
                }
                if !is_group_name(&name) {
                    return Err(self.error("bad character in group name"));
                }
                if self.names.contains(&name) {
                    return Err(self.error("redefinition of group name"));
                }
                self.names.push(name.clone());
                self.capture(Some(name))
            }
            Some(c @ ('=' | '!')) => self.look(false, c == '!'),
            Some('<') => match self.next() {
                Some(c @ ('=' | '!')) => self.look(true, c == '!'),
                _ => Err(self.error("unknown extension ?<")),
            },
            _ => Err(self.error("unsupported group extension")),
        }
    }

    fn capture(&mut self, name: Option<String>) -> Result<Node, Error> {
        let node = self.alternation()?;
        self.close()?;
        Ok(Node::Capture(name, Box::new(node)))
    }

    fn look(&mut self, behind: bool, negated: bool) -> Result<Node, Error> {
        let node = self.alternation()?;
        self.close()?;
        if behind && width(&node).is_none() {
            return Err(self.error("look-behind requires fixed-width pattern"));
        }
        Ok(Node::Look {
            behind,
            negated,
            node: Box::new(node),
        })
    }

    fn close(&mut self) -> Result<(), Error> {
        if self.eat(')') {
            Ok(())
        } else {
            Err(self.error("missing ), unterminated subpattern"))
        }
    }

    fn escape(&mut self) -> Result<Node, Error> {
        let Some(c) = self.next() else {
            return Err(self.error("bad escape (end of pattern)"));
        };
        Ok(match c {
            'A' => Node::Start,
            'Z' => Node::EndOfText,
            'b' => Node::WordBoundary,
            'd' | 'D' | 's' | 'S' | 'w' | 'W' => Node::Set(Set {
                negated: false,
                items: vec![category(c)],
            }),
            _ => Node::Literal(self.char_escape(c)?),
        })
    }

    /// The character an escape outside the category letters stands for.
    fn char_escape(&mut self, c: char) -> Result<char, Error> {
        let digits = match c {
            'a' => return Ok('\x07'),
            'f' => return Ok('\x0c'),
            'n' => return Ok('\n'),
            'r' => return Ok('\r'),
            't' => return Ok('\t'),
            'v' => return Ok('\x0b'),
            'x' => 2,
            'u' => 4,
            'U' => 8,
            c if c.is_ascii_alphanumeric() => {
                return Err(self.error(&format!("bad or unsupported escape \\{c}")));
            }
            c => return Ok(c),
        };
        let hex: String = self.chars.iter().skip(self.pos).take(digits).collect();
        if hex.len() != digits || !hex.chars().all(|c| c.is_ascii_hexdigit()) {
            return Err(self.error("incomplete escape"));
        }
        self.pos += digits;
        u32::from_str_radix(&hex, 16)
            .ok()
            .and_then(char::from_u32)
            .ok_or_else(|| self.error("bad escape"))
    }

    fn set(&mut self) -> Result<Set, Error> {
        let negated = self.eat('^');
        let mut items = Vec::new();
        loop {
            let Some(c) = self.next() else {
                return Err(self.error("unterminated character set"));
            };
            if c == ']' && !items.is_empty() {
                break;
            }
            let first = self.set_member(c)?;
            if !self.eat('-') {
                items.push(first);
                continue;
            }
            match self.next() {
                None => return Err(self.error("unterminated character set")),
                Some(']') => {
                    items.push(first);
                    items.push(Item::Range('-', '-'));
                    break;
                }
                Some(c) => match (first, self.set_member(c)?) {
                    (Item::Range(low, _), Item::Range(high, _)) if low <= high => {
                        items.push(Item::Range(low, high));
                    }
                    _ => return Err(self.error("bad character range")),
                },
            }
        }
        Ok(Set { negated, items })
    }

    /// One member of a set, a character as a one-character range.
    fn set_member(&mut self, c: char) -> Result<Item, Error> {
        if c != '\\' {
            return Ok(Item::Range(c, c));
        }
        let Some(c) = self.next() else {
            return Err(self.error("unterminated character set"));
        };
        let c = match c {
            'd' | 'D' | 's' | 'S' | 'w' | 'W' => return Ok(category(c)),
            'b' => '\x08',
            c => self.char_escape(c)?,
        };
        Ok(Item::Range(c, c))
    }
}

fn category(letter: char) -> Item {
    let category = match letter.to_ascii_lowercase() {
        'd' => Category::Digit,
        's' => Category::Space,
        _ => Category::Word,
    };
    Item::Category(category, letter.is_ascii_uppercase())
}

/// Python's rule for group names: an identifier, here limited to ASCII.
fn is_group_name(name: &str) -> bool {
    let mut chars = name.chars();
    chars
        .next()
        .is_some_and(|c| c == '_' || c.is_ascii_alphabetic())
        && chars.all(|c| c == '_' || c.is_ascii_alphanumeric())
}

/// How many characters every match of `node` has, or None when that varies.
fn width(node: &Node) -> Option<u64> {
    match node {
        Node::Literal(_) | Node::Set(_) => Some(1),
        Node::Start | Node::End | Node::EndOfText | Node::WordBoundary | Node::Look { .. } => {
            Some(0)
        }
        Node::Capture(_, node) | Node::Group(node) => width(node),
        Node::Repeat { node, min, max, .. } => {
            let one = width(node)?;
            (one == 0 || Some(*min) == *max).then(|| one * u64::from(*min))
        }
        Node::Concat(nodes) => nodes.iter().map(width).sum(),
        Node::Alternation(branches) => {
            let first = width(branches.first()?)?;
            branches
                .iter()
                .all(|branch| width(branch) == Some(first))
                .then_some(first)
        }
    }
}
