//! JSON by position: where the members of objects are in a text, so that one member can be added
//! or removed and every other byte of the file stays as it was.
//!
//! It reads JSON, and JSON with comments and trailing commas, as editors write their settings.
//! What it adds is plain JSON, valid in both.

use std::ops::Range;

use serde_json::{Map, Value};

/// How deep a document may nest. Deeper is refused, so a hostile file cannot exhaust the stack.
const MAX_DEPTH: usize = 128;
const BYTE_ORDER_MARK: [u8; 3] = [0xEF, 0xBB, 0xBF];

/// The text is not JSON, with or without comments.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Malformed;

#[derive(Debug, Clone)]
pub struct Member {
    pub key: String,
    /// Where the opening quote of the key is.
    pub key_at: usize,
    /// Where the value ends.
    pub end: usize,
    /// Where the comma after the value is, when there is one.
    pub comma: Option<usize>,
    /// The value, when it is an object.
    pub object: Option<Object>,
}

#[derive(Debug, Clone)]
pub struct Object {
    pub open: usize,
    pub close: usize,
    pub members: Vec<Member>,
}

impl Object {
    /// The members with this key. More than one is possible in a file written by hand.
    pub fn named<'a>(&'a self, key: &str) -> Vec<(usize, &'a Member)> {
        self.members
            .iter()
            .enumerate()
            .filter(|(_, member)| member.key == key)
            .collect()
    }
}

/// A document: the positions of its objects, and its value.
#[derive(Debug, Clone)]
pub struct Document {
    pub root: Object,
    pub value: Value,
}

/// A change of a text: the bytes to take out and the text to put in their place.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Edit {
    pub range: Range<usize>,
    pub text: String,
}

/// How an added member is laid out, taken from the lines around it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Layout {
    /// The indent of the line the member starts on.
    pub indent: String,
    /// One more level of indent.
    pub unit: String,
    pub newline: &'static str,
}

struct Scanner<'a> {
    text: &'a str,
    bytes: &'a [u8],
    at: usize,
}

impl Scanner<'_> {
    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.at).copied()
    }

    fn need(&self) -> Result<u8, Malformed> {
        self.peek().ok_or(Malformed)
    }

    /// Passes white space and comments.
    fn skip(&mut self) -> Result<(), Malformed> {
        loop {
            match self.peek() {
                Some(b' ' | b'\t' | b'\n' | b'\r') => self.at += 1,
                Some(b'/') => self.comment()?,
                _ => return Ok(()),
            }
        }
    }

    fn comment(&mut self) -> Result<(), Malformed> {
        let rest = self.text.get(self.at..).ok_or(Malformed)?;
        if rest.starts_with("//") {
            self.at += rest.find('\n').unwrap_or(rest.len());
            return Ok(());
        }
        if !rest.starts_with("/*") {
            return Err(Malformed);
        }
        let end = rest.get(2..).and_then(|body| body.find("*/"));
        self.at += end.ok_or(Malformed)? + 4;
        Ok(())
    }

    fn string(&mut self) -> Result<String, Malformed> {
        let start = self.at;
        if self.peek() != Some(b'"') {
            return Err(Malformed);
        }
        self.at += 1;
        loop {
            match self.need()? {
                b'"' => break,
                b'\\' => self.at += 2,
                _ => self.at += 1,
            }
        }
        self.at += 1;
        let quoted = self.text.get(start..self.at).ok_or(Malformed)?;
        serde_json::from_str(quoted).map_err(|_| Malformed)
    }

    /// A number, `true`, `false` or `null`.
    fn scalar(&mut self) -> Result<Value, Malformed> {
        let start = self.at;
        while matches!(
            self.peek(),
            Some(b'0'..=b'9' | b'a'..=b'z' | b'A'..=b'Z' | b'+' | b'-' | b'.')
        ) {
            self.at += 1;
        }
        let word = self.text.get(start..self.at).ok_or(Malformed)?;
        serde_json::from_str(word).map_err(|_| Malformed)
    }

    fn value(&mut self, depth: usize) -> Result<(Value, Option<Object>), Malformed> {
        if depth > MAX_DEPTH {
            return Err(Malformed);
        }
        match self.need()? {
            b'{' => {
                let (object, map) = self.object(depth)?;
                Ok((Value::Object(map), Some(object)))
            }
            b'[' => Ok((Value::Array(self.array(depth)?), None)),
            b'"' => Ok((Value::String(self.string()?), None)),
            _ => Ok((self.scalar()?, None)),
        }
    }

    fn array(&mut self, depth: usize) -> Result<Vec<Value>, Malformed> {
        self.at += 1;
        let mut items = Vec::new();
        loop {
            self.skip()?;
            if self.peek() == Some(b']') {
                self.at += 1;
                return Ok(items);
            }
            items.push(self.value(depth + 1)?.0);
            self.skip()?;
            match self.need()? {
                b',' => self.at += 1,
                b']' => {}
                _ => return Err(Malformed),
            }
        }
    }

    fn object(&mut self, depth: usize) -> Result<(Object, Map<String, Value>), Malformed> {
        let open = self.at;
        self.at += 1;
        let mut members = Vec::new();
        let mut map = Map::new();
        loop {
            self.skip()?;
            if self.peek() == Some(b'}') {
                let close = self.at;
                self.at += 1;
                return Ok((
                    Object {
                        open,
                        close,
                        members,
                    },
                    map,
                ));
            }
            let member = self.member(depth, &mut map)?;
            let last = member.comma.is_none();
            members.push(member);
            self.skip()?;
            if last && self.peek() != Some(b'}') {
                return Err(Malformed);
            }
        }
    }

    fn member(&mut self, depth: usize, map: &mut Map<String, Value>) -> Result<Member, Malformed> {
        let key_at = self.at;
        let key = self.string()?;
        self.skip()?;
        if self.peek() != Some(b':') {
            return Err(Malformed);
        }
        self.at += 1;
        self.skip()?;
        let (value, object) = self.value(depth + 1)?;
        let end = self.at;
        map.insert(key.clone(), value);
        self.skip()?;
        let comma = (self.peek() == Some(b',')).then_some(self.at);
        if comma.is_some() {
            self.at += 1;
        }
        Ok(Member {
            key,
            key_at,
            end,
            comma,
            object,
        })
    }
}

/// Reads a document whose root is an object.
pub fn parse(text: &str) -> Result<Document, Malformed> {
    let bytes = text.as_bytes();
    let mut scanner = Scanner {
        text,
        bytes,
        at: if bytes.starts_with(&BYTE_ORDER_MARK) {
            BYTE_ORDER_MARK.len()
        } else {
            0
        },
    };
    scanner.skip()?;
    if scanner.peek() != Some(b'{') {
        return Err(Malformed);
    }
    let (root, map) = scanner.object(0)?;
    scanner.skip()?;
    if scanner.at != text.len() {
        return Err(Malformed);
    }
    Ok(Document {
        root,
        value: Value::Object(map),
    })
}

fn line_start(text: &str, at: usize) -> usize {
    text.get(..at)
        .and_then(|before| before.rfind('\n'))
        .map_or(0, |found| found + 1)
}

/// The white space before `at` on its line, when nothing else is before it there.
fn indent_before(text: &str, at: usize) -> Option<&str> {
    let lead = text.get(line_start(text, at)..at)?;
    lead.chars()
        .all(|character| character == ' ' || character == '\t')
        .then_some(lead)
}

/// The white space a line starts with.
fn indent_of_line(text: &str, at: usize) -> &str {
    let line = text.get(line_start(text, at)..).unwrap_or("");
    let width = line.len() - line.trim_start_matches([' ', '\t']).len();
    line.get(..width).unwrap_or("")
}

/// How a member added to `object` is laid out: as its other members are, or one level inside its
/// closing brace.
pub fn layout(text: &str, object: &Object) -> Layout {
    let closing = indent_before(text, object.close)
        .unwrap_or_else(|| indent_of_line(text, object.open))
        .to_owned();
    let first = object.members.first();
    let found = first.and_then(|member| indent_before(text, member.key_at));
    let step = if closing.contains('\t') { "\t" } else { "  " };
    let indent = found.map_or_else(|| format!("{closing}{step}"), str::to_owned);
    let unit = indent
        .strip_prefix(closing.as_str())
        .filter(|rest| !rest.is_empty())
        .unwrap_or(step)
        .to_owned();
    Layout {
        indent,
        unit,
        newline: if text.contains("\r\n") { "\r\n" } else { "\n" },
    }
}

/// The edit that adds `member` (a key, a colon and a value) to `object` as its last member.
pub fn insert(text: &str, object: &Object, layout: &Layout, member: &str) -> Edit {
    let (newline, indent) = (layout.newline, &layout.indent);
    let Some(last) = object.members.last() else {
        let inside = text.get(object.open + 1..object.close).unwrap_or("");
        if !inside.trim().is_empty() {
            // Comments only: the member goes before them.
            return Edit {
                range: object.open + 1..object.open + 1,
                text: format!("{newline}{indent}{member}"),
            };
        }
        let closing = indent.strip_suffix(layout.unit.as_str()).unwrap_or("");
        return Edit {
            range: object.open + 1..object.close,
            text: format!("{newline}{indent}{member}{newline}{closing}"),
        };
    };
    match last.comma {
        // The file ends its lists with a comma: so does the member, and taking it out again
        // gives the file back as it was.
        Some(comma) => Edit {
            range: comma + 1..comma + 1,
            text: format!("{newline}{indent}{member},"),
        },
        None => Edit {
            range: last.end..last.end,
            text: format!(",{newline}{indent}{member}"),
        },
    }
}

/// The edits that take the member at `index` out of `object`: its lines, and the comma that
/// would be left behind before the closing brace.
pub fn remove(text: &str, object: &Object, index: usize) -> Vec<Edit> {
    let Some(member) = object.members.get(index) else {
        return Vec::new();
    };
    let whole_line = indent_before(text, member.key_at).is_some();
    let start = if whole_line {
        line_start(text, member.key_at)
    } else {
        member.key_at
    };
    let mut end = member.comma.map_or(member.end, |comma| comma + 1);
    if whole_line {
        let rest = text.get(end..).unwrap_or("");
        let blank = rest.len() - rest.trim_start_matches([' ', '\t']).len();
        let after = rest.get(blank..).unwrap_or("");
        if after.starts_with("\r\n") {
            end += blank + 2;
        } else if after.starts_with('\n') {
            end += blank + 1;
        }
    }
    let mut edits = vec![Edit {
        range: start..end,
        text: String::new(),
    }];
    let before = index.checked_sub(1).and_then(|at| object.members.get(at));
    if let (None, Some(comma)) = (member.comma, before.and_then(|previous| previous.comma)) {
        edits.push(Edit {
            range: comma..comma + 1,
            text: String::new(),
        });
    }
    edits
}

/// The text with the edits made. The edits must not overlap.
pub fn apply(text: &str, mut edits: Vec<Edit>) -> String {
    edits.sort_by_key(|edit| std::cmp::Reverse(edit.range.start));
    let mut out = text.to_owned();
    for edit in edits {
        if out.get(edit.range.clone()).is_some() {
            out.replace_range(edit.range, &edit.text);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const SETTINGS: &str = "{\n  // Servers of this editor.\n  \"servers\": {\n    \"first\": { \"url\": \"https://one.example\" }\n  },\n  \"theme\": \"dark\",\n}\n";

    fn servers(document: &Document) -> &Object {
        let found = document.root.named("servers");
        found
            .first()
            .and_then(|(_, member)| member.object.as_ref())
            .expect("an object")
    }

    #[test]
    fn a_document_with_comments_and_trailing_commas_is_read_with_its_value() {
        let document = parse(SETTINGS).expect("read");

        assert_eq!(
            document.value,
            json!({"servers": {"first": {"url": "https://one.example"}}, "theme": "dark"})
        );
    }

    #[test]
    fn a_member_is_added_after_the_last_one_in_the_layout_of_its_neighbours() {
        let document = parse(SETTINGS).expect("read");
        let object = servers(&document);

        let edit = insert(SETTINGS, object, &layout(SETTINGS, object), "\"second\": 2");
        let changed = apply(SETTINGS, vec![edit]);

        assert!(changed.contains(
            "    \"first\": { \"url\": \"https://one.example\" },\n    \"second\": 2\n  },"
        ));
    }

    #[test]
    fn removing_an_added_member_gives_the_text_back_byte_for_byte() {
        let document = parse(SETTINGS).expect("read");
        let object = servers(&document);
        let edit = insert(SETTINGS, object, &layout(SETTINGS, object), "\"second\": 2");
        let changed = apply(SETTINGS, vec![edit]);
        let again = parse(&changed).expect("read");
        let object = servers(&again);

        let restored = apply(&changed, remove(&changed, object, 1));

        assert_eq!(restored, SETTINGS);
    }

    #[test]
    fn a_member_goes_into_an_empty_object_on_a_line_of_its_own() {
        let text = "{\n\t\"servers\": {}\n}\n";
        let document = parse(text).expect("read");
        let object = servers(&document);

        let changed = apply(
            text,
            vec![insert(text, object, &layout(text, object), "\"one\": 1")],
        );

        assert_eq!(changed, "{\n\t\"servers\": {\n\t\t\"one\": 1\n\t}\n}\n");
    }

    #[test]
    fn a_member_in_the_middle_goes_with_its_own_comma_only() {
        let text = "{\n  \"a\": 1,\n  \"b\": 2,\n  \"c\": 3\n}\n";
        let document = parse(text).expect("read");

        let changed = apply(text, remove(text, &document.root, 1));

        assert_eq!(changed, "{\n  \"a\": 1,\n  \"c\": 3\n}\n");
    }

    #[test]
    fn windows_line_ends_are_kept() {
        let text = "{\r\n  \"a\": 1\r\n}\r\n";
        let document = parse(text).expect("read");
        let root = &document.root;

        let changed = apply(
            text,
            vec![insert(text, root, &layout(text, root), "\"b\": 2")],
        );

        assert_eq!(changed, "{\r\n  \"a\": 1,\r\n  \"b\": 2\r\n}\r\n");
    }

    #[test]
    fn text_that_is_not_json_is_refused() {
        let broken = [
            "",
            "[]",
            "{",
            "{\"a\": }",
            "{\"a\": 1 \"b\": 2}",
            "{\"a\": 1} trailing",
            "{\"a\": 'single'}",
            "{\"a\": 1 /* open",
            "{\"a\": \"open",
            "{a: 1}",
        ];

        let refused = broken.iter().filter(|text| parse(text).is_err()).count();

        assert_eq!(refused, broken.len());
    }

    #[test]
    fn a_document_nested_too_deep_is_refused() {
        let text = format!("{}1{}", "{\"a\":".repeat(200), "}".repeat(200));

        let answer = parse(&text);

        assert!(answer.is_err());
    }
}
