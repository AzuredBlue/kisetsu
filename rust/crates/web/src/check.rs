//! Checking what a request carries: path, query and body values are coerced to the types a
//! route wants, and every problem found is reported at once, in the shape the UI reads.

use serde_json::{Value, json};

/// Where in the request a value was.
#[derive(Debug, Clone, PartialEq)]
pub enum Place {
    Key(String),
    Index(usize),
}

impl Place {
    fn to_json(&self) -> Value {
        match self {
            Self::Key(key) => json!(key),
            Self::Index(index) => json!(index),
        }
    }
}

/// One thing wrong with a request.
#[derive(Debug, Clone, PartialEq)]
pub struct Problem {
    pub kind: &'static str,
    pub place: Vec<Place>,
    pub message: String,
    pub input: Value,
    pub context: Option<Value>,
}

impl Problem {
    fn to_json(&self) -> Value {
        let mut out = json!({
            "type": self.kind,
            "loc": self.place.iter().map(Place::to_json).collect::<Vec<_>>(),
            "msg": self.message,
            "input": self.input,
        });
        if let (Some(context), Some(map)) = (&self.context, out.as_object_mut()) {
            map.insert("ctx".to_owned(), context.clone());
        }
        out
    }
}

/// The problems found so far in one request.
#[derive(Debug, Default)]
pub struct Problems(Vec<Problem>);

impl Problems {
    pub fn push(&mut self, problem: Problem) {
        self.0.push(problem);
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// The body of the 422 answer.
    pub fn to_json(&self) -> Value {
        json!({"detail": self.0.iter().map(Problem::to_json).collect::<Vec<_>>()})
    }
}

/// What a value was wrong with, before it is placed in the request.
struct Fault {
    kind: &'static str,
    message: String,
    context: Option<Value>,
}

impl Fault {
    fn new(kind: &'static str, message: &str) -> Self {
        Self {
            kind,
            message: message.to_owned(),
            context: None,
        }
    }
}

type Checked<T> = Result<T, Fault>;

fn integer_from_text(text: &str) -> Option<i64> {
    let text = text.trim();
    let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    text.parse().ok()
}

fn number_from_text(text: &str) -> Option<f64> {
    let text = text.trim();
    let plain = !text.is_empty()
        && text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'+' | b'-' | b'.' | b'e' | b'E'));
    if plain { text.parse().ok() } else { None }
}

/// An integer: a whole number, a whole float, a boolean or text that reads as one.
fn integer(value: &Value) -> Checked<i64> {
    match value {
        Value::Number(number) => match number.as_i64() {
            Some(whole) => Ok(whole),
            None => match number.as_f64() {
                Some(float) if float.fract() == 0.0 && float.abs() < 9.2e18 => Ok(float as i64),
                Some(_) => Err(Fault::new(
                    "int_from_float",
                    "Input should be a valid integer, got a number with a fractional part",
                )),
                None => Err(Fault::new("int_parsing", "Input should be a valid integer")),
            },
        },
        Value::Bool(flag) => Ok(i64::from(*flag)),
        Value::String(text) => integer_from_text(text).ok_or_else(|| {
            Fault::new(
                "int_parsing",
                "Input should be a valid integer, unable to parse string as an integer",
            )
        }),
        _ => Err(Fault::new("int_type", "Input should be a valid integer")),
    }
}

fn float(value: &Value) -> Checked<f64> {
    match value {
        Value::Number(number) => number
            .as_f64()
            .ok_or_else(|| Fault::new("float_type", "Input should be a valid number")),
        Value::Bool(flag) => Ok(f64::from(u8::from(*flag))),
        Value::String(text) => number_from_text(text).ok_or_else(|| {
            Fault::new(
                "float_parsing",
                "Input should be a valid number, unable to parse string as a number",
            )
        }),
        _ => Err(Fault::new("float_type", "Input should be a valid number")),
    }
}

fn boolean(value: &Value) -> Checked<bool> {
    const TRUE: [&str; 6] = ["1", "true", "t", "yes", "y", "on"];
    const FALSE: [&str; 6] = ["0", "false", "f", "no", "n", "off"];
    let unreadable = || {
        Fault::new(
            "bool_parsing",
            "Input should be a valid boolean, unable to interpret input",
        )
    };
    match value {
        Value::Bool(flag) => Ok(*flag),
        Value::String(text) => {
            let text = text.trim().to_lowercase();
            if TRUE.contains(&text.as_str()) {
                Ok(true)
            } else if FALSE.contains(&text.as_str()) {
                Ok(false)
            } else {
                Err(unreadable())
            }
        }
        Value::Number(number) => match number.as_i64() {
            Some(1) => Ok(true),
            Some(0) => Ok(false),
            _ => Err(unreadable()),
        },
        _ => Err(Fault::new("bool_type", "Input should be a valid boolean")),
    }
}

fn string(value: &Value) -> Checked<String> {
    value
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| Fault::new("string_type", "Input should be a valid string"))
}

/// Reads values of one part of a request (its path, query or body) and notes what is wrong.
pub struct Reader<'p> {
    problems: &'p mut Problems,
    base: Vec<Place>,
}

impl<'p> Reader<'p> {
    pub fn new(problems: &'p mut Problems, part: &str) -> Self {
        Self {
            problems,
            base: vec![Place::Key(part.to_owned())],
        }
    }

    fn fail(&mut self, name: &str, input: &Value, fault: Fault) {
        let mut place = self.base.clone();
        place.push(Place::Key(name.to_owned()));
        self.problems.push(Problem {
            kind: fault.kind,
            place,
            message: fault.message,
            input: input.clone(),
            context: fault.context,
        });
    }

    fn missing(&mut self, name: &str, input: &Value) {
        self.fail(name, input, Fault::new("missing", "Field required"));
    }

    /// Reads one field with `read`; a field that is absent gives `None` and, when `required`,
    /// a problem.
    fn field<T>(
        &mut self,
        object: &Value,
        name: &str,
        required: bool,
        read: impl FnOnce(&Value) -> Checked<T>,
    ) -> Option<T> {
        let Some(value) = object.get(name) else {
            if required {
                self.missing(name, object);
            }
            return None;
        };
        match read(value) {
            Ok(read) => Some(read),
            Err(fault) => {
                self.fail(name, value, fault);
                None
            }
        }
    }

    /// Like [`Self::field`], but `null` counts as absent.
    fn optional<T>(
        &mut self,
        object: &Value,
        name: &str,
        read: impl FnOnce(&Value) -> Checked<T>,
    ) -> Option<T> {
        match object.get(name) {
            Some(Value::Null) | None => None,
            Some(_) => self.field(object, name, false, read),
        }
    }

    pub fn required_integer(&mut self, object: &Value, name: &str) -> Option<i64> {
        self.field(object, name, true, integer)
    }

    pub fn optional_integer(&mut self, object: &Value, name: &str) -> Option<i64> {
        self.optional(object, name, integer)
    }

    pub fn required_string(&mut self, object: &Value, name: &str) -> Option<String> {
        self.field(object, name, true, string)
    }

    /// A text that may be absent but not `null`.
    pub fn text_or_absent(&mut self, object: &Value, name: &str) -> Option<String> {
        self.field(object, name, false, string)
    }

    pub fn optional_string(&mut self, object: &Value, name: &str) -> Option<String> {
        self.optional(object, name, string)
    }

    pub fn optional_float(&mut self, object: &Value, name: &str) -> Option<f64> {
        self.optional(object, name, float)
    }

    /// A boolean that is `false` when absent.
    pub fn flag(&mut self, object: &Value, name: &str) -> bool {
        self.field(object, name, false, boolean).unwrap_or(false)
    }

    pub fn optional_flag(&mut self, object: &Value, name: &str) -> Option<bool> {
        self.optional(object, name, boolean)
    }

    pub fn required_integers(&mut self, object: &Value, name: &str) -> Option<Vec<i64>> {
        self.list(object, name, true, integer)
    }

    pub fn required_strings(&mut self, object: &Value, name: &str) -> Option<Vec<String>> {
        self.list(object, name, true, string)
    }

    pub fn optional_strings(&mut self, object: &Value, name: &str) -> Option<Vec<String>> {
        match object.get(name) {
            Some(Value::Null) | None => None,
            Some(_) => self.list(object, name, false, string),
        }
    }

    fn list<T>(
        &mut self,
        object: &Value,
        name: &str,
        required: bool,
        read: impl Fn(&Value) -> Checked<T>,
    ) -> Option<Vec<T>> {
        let Some(value) = object.get(name) else {
            if required {
                self.missing(name, object);
            }
            return None;
        };
        let Some(items) = value.as_array() else {
            self.fail(
                name,
                value,
                Fault::new("list_type", "Input should be a valid list"),
            );
            return None;
        };
        let mut all = Vec::with_capacity(items.len());
        let mut sound = true;
        for (index, item) in items.iter().enumerate() {
            match read(item) {
                Ok(read) => all.push(read),
                Err(fault) => {
                    sound = false;
                    let mut place = self.base.clone();
                    place.push(Place::Key(name.to_owned()));
                    place.push(Place::Index(index));
                    self.problems.push(Problem {
                        kind: fault.kind,
                        place,
                        message: fault.message,
                        input: item.clone(),
                        context: fault.context,
                    });
                }
            }
        }
        sound.then_some(all)
    }

    /// A value the field's own rule refused: a `value_error` carrying the reason.
    pub fn refuse_value(&mut self, name: &str, input: &str, reason: &str) {
        self.fail(
            name,
            &json!(input),
            Fault {
                kind: "value_error",
                message: format!("Value error, {reason}"),
                context: Some(json!({"error": {}})),
            },
        );
    }

    /// A number below the least the field allows.
    pub fn refuse_below(&mut self, name: &str, input: i64, least: i64) {
        self.fail(
            name,
            &json!(input),
            Fault {
                kind: "greater_than_equal",
                message: format!("Input should be greater than or equal to {least}"),
                context: Some(json!({"ge": least})),
            },
        );
    }

    /// A number above the most the field allows.
    pub fn refuse_above(&mut self, name: &str, input: i64, most: i64) {
        self.fail(
            name,
            &json!(input),
            Fault {
                kind: "less_than_equal",
                message: format!("Input should be less than or equal to {most}"),
                context: Some(json!({"le": most})),
            },
        );
    }

    /// A decimal below the least the field allows.
    pub fn refuse_below_number(&mut self, name: &str, input: f64, least: i64) {
        self.fail(
            name,
            &json!(input),
            Fault {
                kind: "greater_than_equal",
                message: format!("Input should be greater than or equal to {least}"),
                context: Some(json!({"ge": least})),
            },
        );
    }

    /// Refuses every field of `object` that is not in `allowed`.
    pub fn forbid_others(&mut self, object: &Value, allowed: &[&str]) {
        let Some(map) = object.as_object() else {
            return;
        };
        for (name, value) in map {
            if !allowed.contains(&name.as_str()) {
                self.fail(
                    name,
                    value,
                    Fault::new("extra_forbidden", "Extra inputs are not permitted"),
                );
            }
        }
    }

    fn query<T>(
        &mut self,
        pairs: &[(String, String)],
        name: &str,
        read: impl FnOnce(&Value) -> Checked<T>,
    ) -> Option<T> {
        let value = json!(query_value(pairs, name)?);
        match read(&value) {
            Ok(read) => Some(read),
            Err(fault) => {
                self.fail(name, &value, fault);
                None
            }
        }
    }

    pub fn query_integer(&mut self, pairs: &[(String, String)], name: &str) -> Option<i64> {
        self.query(pairs, name, integer)
    }

    pub fn query_flag(&mut self, pairs: &[(String, String)], name: &str) -> bool {
        self.query(pairs, name, boolean).unwrap_or(false)
    }

    /// A text the route cannot do without.
    pub fn required_query_text(
        &mut self,
        pairs: &[(String, String)],
        name: &str,
    ) -> Option<String> {
        let found = query_value(pairs, name).map(str::to_owned);
        if found.is_none() {
            self.missing(name, &Value::Null);
        }
        found
    }

    /// A path value that must be a whole number.
    pub fn path_integer(&mut self, name: &str, raw: &str) -> Option<i64> {
        match integer(&json!(raw)) {
            Ok(read) => Some(read),
            Err(fault) => {
                self.fail(name, &json!(raw), fault);
                None
            }
        }
    }
}

/// A query parameter, from the pairs of the request; the last of a repeated one counts. A
/// parameter with no value reads as the empty text.
pub fn query_value<'a>(pairs: &'a [(String, String)], name: &str) -> Option<&'a str> {
    pairs
        .iter()
        .rev()
        .find(|(key, _)| key == name)
        .map(|(_, value)| value.as_str())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn problems_of(body: &Value, read: impl FnOnce(&mut Reader<'_>, &Value)) -> Vec<String> {
        let mut problems = Problems::default();
        read(&mut Reader::new(&mut problems, "body"), body);
        problems
            .0
            .iter()
            .map(|p| format!("{} {:?}", p.kind, p.place))
            .collect()
    }

    #[test]
    fn integers_are_read_the_way_a_form_would_send_them() {
        for good in [json!(1), json!(1.0), json!("1"), json!(true), json!(" 7 ")] {
            assert!(integer(&good).is_ok(), "{good}");
        }
        assert_eq!(
            integer(&json!(1.5)).map_err(|f| f.kind),
            Err("int_from_float")
        );
        assert_eq!(
            integer(&json!("1.5")).map_err(|f| f.kind),
            Err("int_parsing")
        );
        assert_eq!(integer(&Value::Null).map_err(|f| f.kind), Err("int_type"));
    }

    #[test]
    fn every_problem_of_a_body_is_reported_with_its_place() {
        let found = problems_of(&json!({"ids": [1, "a"], "extra": 1}), |reader, body| {
            reader.required_integers(body, "ids");
            reader.required_string(body, "name");
            reader.forbid_others(body, &["ids", "name"]);
        });
        assert_eq!(
            found,
            [
                r#"int_parsing [Key("body"), Key("ids"), Index(1)]"#,
                r#"missing [Key("body"), Key("name")]"#,
                r#"extra_forbidden [Key("body"), Key("extra")]"#,
            ]
        );
    }

    #[test]
    fn booleans_accept_the_usual_words_and_nothing_else() {
        assert_eq!(boolean(&json!("TRUE")).ok(), Some(true));
        assert_eq!(boolean(&json!("no")).ok(), Some(false));
        assert!(boolean(&json!("maybe")).is_err());
        assert!(boolean(&json!("")).is_err());
    }
}
