//! The read tools the Data screen may run, and how a call of one becomes a request.
//!
//! The screen is read-only, and that is decided here, not in the front end:
//!
//! - A tool is one of [`READ_TOOLS`]: a name and the `GET` route of the HTTP API that serves it.
//!   The app sends `GET` to these routes and nothing else; no route that writes is in the table.
//! - The front end names a tool and gives values. Values for the route's `{placeholders}` become
//!   encoded path segments; everything else becomes query pairs. A value cannot change the route.
//! - Which arguments a tool takes is the server's to say: the form is built from the server's
//!   OpenAPI document ([`describe_tools`]), so a filter the server adds appears without a change
//!   here, and a tool the server does not have is not offered.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::error::{clean, AppError, AppResult};

/// Each read tool and the route that serves it. `list_commits` is offered once a server has it.
pub const READ_TOOLS: &[(&str, &str)] = &[
    ("search", "/v1/search"),
    ("search_code", "/v1/search/code"),
    ("get_symbol", "/v1/symbols"),
    (
        "read_source",
        "/v1/workspaces/{workspace}/sources/{source}/files/{path}",
    ),
    ("list_decisions", "/v1/decisions"),
    ("get_decision", "/v1/decisions/{item_id}"),
    ("list_meetings", "/v1/meetings"),
    ("get_meeting", "/v1/meetings/{record_id}"),
    ("list_messages", "/v1/messages"),
    ("get_thread", "/v1/threads/{record_id}"),
    ("list_commits", "/v1/commits"),
];

/// The placeholders that take a path of their own: such a value spans segments and may be empty
/// (the root of a repository). Every other placeholder is one segment and not empty.
const SPANNING: &[(&str, &str)] = &[("read_source", "path")];

/// How many `$ref`s are followed inside one parameter before the rest is left as it is.
const MAX_REFERENCE_HOPS: usize = 8;
const SCHEMA_PREFIX: &str = "#/components/schemas/";

const MAX_QUERY_PAIRS: usize = 200;
const MAX_NAME_LENGTH: usize = 64;
const MAX_VALUE_LENGTH: usize = 8192;

/// A read tool as the server publishes it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ToolDescription {
    pub name: &'static str,
    /// The route's template, with its `{placeholders}`.
    pub path: &'static str,
    pub summary: String,
    pub description: String,
    /// The parameters of the operation as the OpenAPI document has them, references resolved.
    pub parameters: Vec<Value>,
    /// The placeholders that hold a path of their own and may be left empty (see [`SPANNING`]).
    pub spanning: Vec<&'static str>,
}

/// The read tools the server's OpenAPI document has a `GET` route for, in the table's order.
pub fn describe_tools(openapi: &Value) -> Vec<ToolDescription> {
    let schemas = openapi.pointer("/components/schemas");
    READ_TOOLS
        .iter()
        .filter_map(|(name, path)| {
            let operation = openapi.get("paths")?.get(path)?.get("get")?;
            let parameters = operation
                .get("parameters")
                .and_then(Value::as_array)
                .map(|list| list.iter().map(|item| resolve(item, schemas, 0)).collect())
                .unwrap_or_default();
            Some(ToolDescription {
                name,
                path,
                summary: text_of(operation.get("summary")),
                description: text_of(operation.get("description")),
                parameters,
                spanning: SPANNING
                    .iter()
                    .filter(|(tool, _)| tool == name)
                    .map(|(_, placeholder)| *placeholder)
                    .collect(),
            })
        })
        .collect()
}

fn text_of(value: Option<&Value>) -> String {
    value.and_then(Value::as_str).map(clean).unwrap_or_default()
}

/// `value` with every `{"$ref": "#/components/schemas/Name"}` replaced by the schema it names.
fn resolve(value: &Value, schemas: Option<&Value>, hops: usize) -> Value {
    match value {
        Value::Object(object) => {
            let referenced = object
                .get("$ref")
                .and_then(Value::as_str)
                .and_then(|reference| reference.strip_prefix(SCHEMA_PREFIX))
                .and_then(|name| schemas?.get(name));
            match referenced {
                Some(target) if hops < MAX_REFERENCE_HOPS => resolve(target, schemas, hops + 1),
                _ => Value::Object(
                    object
                        .iter()
                        .map(|(key, item)| (key.clone(), resolve(item, schemas, hops)))
                        .collect(),
                ),
            }
        }
        Value::Array(items) => Value::Array(
            items
                .iter()
                .map(|item| resolve(item, schemas, hops))
                .collect(),
        ),
        other => other.clone(),
    }
}

/// What the front end asks to run: a tool by name, values for the route's placeholders, and the
/// arguments that go into the query, a repeated name for a list.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct ToolCall {
    pub tool: String,
    #[serde(default)]
    pub path: BTreeMap<String, String>,
    #[serde(default)]
    pub query: Vec<(String, String)>,
}

/// The request a call stands for: path segments under the server's address, and query pairs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Route {
    pub segments: Vec<String>,
    pub query: Vec<(String, String)>,
}

/// The route of a call, or why the call cannot be made.
pub fn route_of(call: &ToolCall) -> AppResult<Route> {
    let template = READ_TOOLS
        .iter()
        .find(|(name, _)| *name == call.tool)
        .map(|(_, path)| *path)
        .ok_or_else(|| AppError::invalid("That is not a read tool this app can run."))?;
    let mut used = 0;
    let mut segments = Vec::new();
    for part in template.trim_start_matches('/').split('/') {
        let Some(name) = placeholder(part) else {
            segments.push(part.to_owned());
            continue;
        };
        let value = call
            .path
            .get(name)
            .ok_or_else(|| AppError::invalid(format!("The call needs a value for {name}.")))?;
        used += 1;
        let spans = SPANNING.contains(&(call.tool.as_str(), name));
        segments.extend(path_segments(name, value, spans)?);
    }
    if used != call.path.len() {
        return Err(AppError::invalid(
            "The call names a path value this tool does not take.",
        ));
    }
    Ok(Route {
        segments,
        query: checked_query(&call.query)?,
    })
}

fn placeholder(part: &str) -> Option<&str> {
    part.strip_prefix('{')?.strip_suffix('}')
}

/// The segments a placeholder's value becomes: one, or for a placeholder of [`SPANNING`] one per
/// part of the path. `.` and `..` are refused: they would name another route.
fn path_segments(name: &str, value: &str, spans: bool) -> AppResult<Vec<String>> {
    if value.len() > MAX_VALUE_LENGTH {
        return Err(AppError::invalid(format!(
            "The value for {name} is too long."
        )));
    }
    if !spans && value.is_empty() {
        return Err(AppError::invalid(format!(
            "The call needs a value for {name}."
        )));
    }
    let segments: Vec<String> = if spans {
        value.split('/').map(str::to_owned).collect()
    } else {
        vec![value.to_owned()]
    };
    if segments
        .iter()
        .any(|segment| segment == "." || segment == "..")
    {
        return Err(AppError::invalid(format!(
            "The value for {name} must not hold a . or .. part."
        )));
    }
    Ok(segments)
}

fn checked_query(query: &[(String, String)]) -> AppResult<Vec<(String, String)>> {
    if query.len() > MAX_QUERY_PAIRS {
        return Err(AppError::invalid("The call has too many arguments."));
    }
    for (name, value) in query {
        let named = !name.is_empty()
            && name.len() <= MAX_NAME_LENGTH
            && name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'));
        if !named {
            return Err(AppError::invalid("An argument's name is not valid."));
        }
        if value.len() > MAX_VALUE_LENGTH {
            return Err(AppError::invalid(format!(
                "The value for {name} is too long."
            )));
        }
    }
    Ok(query.to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::ErrorKind;
    use serde_json::json;

    fn call(tool: &str, path: &[(&str, &str)], query: &[(&str, &str)]) -> ToolCall {
        ToolCall {
            tool: tool.to_owned(),
            path: path
                .iter()
                .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
                .collect(),
            query: query
                .iter()
                .map(|(name, value)| ((*name).to_owned(), (*value).to_owned()))
                .collect(),
        }
    }

    fn openapi() -> Value {
        json!({
            "paths": {
                "/v1/search": {
                    "get": {
                        "summary": "Search everything",
                        "description": "Ranked search.\nOver every kind of record.",
                        "parameters": [
                            {"name": "query", "in": "query", "required": true,
                             "schema": {"type": "string"}},
                            {"name": "mode", "in": "query", "required": false,
                             "schema": {"$ref": "#/components/schemas/SearchMode"}}
                        ]
                    },
                    "post": {"summary": "Not a read"}
                },
                "/v1/decisions/{item_id}": {
                    "get": {"summary": "Get a decision", "parameters": [
                        {"name": "item_id", "in": "path", "required": true,
                         "schema": {"type": "string", "format": "uuid"}}
                    ]}
                },
                "/v1/workspaces/{workspace}/decisions": {
                    "post": {"summary": "Record a decision"}
                },
                "/v1/tokens": {"get": {"summary": "List tokens"}}
            },
            "components": {"schemas": {
                "SearchMode": {"type": "string", "enum": ["hybrid", "dense"], "default": "hybrid"},
                "Loop": {"$ref": "#/components/schemas/Loop"}
            }}
        })
    }

    #[test]
    fn every_tool_of_the_table_is_a_route_under_v1() {
        for (name, path) in READ_TOOLS {
            assert!(path.starts_with("/v1/"), "{name}");
            assert!(!name.is_empty());
        }
        let names: Vec<&str> = READ_TOOLS.iter().map(|(name, _)| *name).collect();
        for written in [
            "add_document",
            "record_decision",
            "supersede_decision",
            "close_decision",
            "retract_decision",
        ] {
            assert!(!names.contains(&written), "{written} writes");
        }
    }

    #[test]
    fn only_the_read_tools_the_server_has_are_described() {
        let tools = describe_tools(&openapi());
        let names: Vec<&str> = tools.iter().map(|tool| tool.name).collect();
        // No list_commits: the document has no route for it. No tokens: not a tool of the table.
        assert_eq!(names, ["search", "get_decision"]);
        assert_eq!(tools[0].summary, "Search everything");
        assert_eq!(
            tools[0].description,
            "Ranked search. Over every kind of record."
        );
        assert_eq!(tools[1].path, "/v1/decisions/{item_id}");
        assert!(tools[1].spanning.is_empty());
    }

    #[test]
    fn a_reference_in_a_parameter_is_replaced_by_its_schema() {
        let tools = describe_tools(&openapi());
        assert_eq!(
            tools[0].parameters[1]["schema"],
            json!({"type": "string", "enum": ["hybrid", "dense"], "default": "hybrid"})
        );
    }

    #[test]
    fn a_reference_that_names_itself_does_not_loop() {
        let schemas = openapi()["components"]["schemas"].clone();
        let resolved = resolve(
            &json!({"$ref": "#/components/schemas/Loop"}),
            Some(&schemas),
            0,
        );
        assert_eq!(resolved, json!({"$ref": "#/components/schemas/Loop"}));
    }

    #[test]
    fn a_document_that_is_not_openapi_describes_no_tool() {
        assert!(describe_tools(&json!({"paths": "none"})).is_empty());
        assert!(describe_tools(&json!([])).is_empty());
    }

    #[test]
    fn a_call_becomes_segments_and_query_pairs() {
        let route = route_of(&call(
            "search",
            &[],
            &[
                ("query", "archive retention"),
                ("kinds", "thread"),
                ("kinds", "meeting"),
            ],
        ))
        .expect("a route");
        assert_eq!(route.segments, ["v1", "search"]);
        assert_eq!(route.query.len(), 3);
        assert_eq!(
            route.query[0],
            ("query".to_owned(), "archive retention".to_owned())
        );
    }

    #[test]
    fn a_file_path_spans_segments_and_the_root_is_a_trailing_slash() {
        let file = route_of(&call(
            "read_source",
            &[
                ("workspace", "harbour"),
                ("source", "harbour-notes"),
                ("path", "docs/a b.md"),
            ],
            &[("start_line", "1")],
        ))
        .expect("a route");
        assert_eq!(
            file.segments,
            [
                "v1",
                "workspaces",
                "harbour",
                "sources",
                "harbour-notes",
                "files",
                "docs",
                "a b.md"
            ]
        );
        let root = route_of(&call(
            "read_source",
            &[
                ("workspace", "harbour"),
                ("source", "harbour-notes"),
                ("path", ""),
            ],
            &[],
        ))
        .expect("a route");
        assert_eq!(root.segments.last().map(String::as_str), Some(""));
    }

    #[test]
    fn a_value_cannot_name_another_route() {
        let slash = route_of(&call(
            "read_source",
            &[
                ("workspace", "harbour/sources/x"),
                ("source", "s"),
                ("path", "a"),
            ],
            &[],
        ))
        .expect("one segment");
        // Kept as one segment, which the URL encodes: it names no other route.
        assert_eq!(slash.segments[2], "harbour/sources/x");

        for path in ["../../tokens", "a/./b", ".."] {
            let error = route_of(&call(
                "read_source",
                &[("workspace", "harbour"), ("source", "s"), ("path", path)],
                &[],
            ))
            .expect_err("refused");
            assert_eq!(error.kind, ErrorKind::Invalid, "{path}");
        }
        assert!(route_of(&call("get_decision", &[("item_id", "..")], &[])).is_err());
        assert!(route_of(&call("get_decision", &[("item_id", "")], &[])).is_err());
    }

    #[test]
    fn a_call_of_something_else_than_a_read_tool_is_refused() {
        for tool in [
            "record_decision",
            "tokens",
            "/v1/tokens",
            "",
            "search/../tokens",
        ] {
            let error = route_of(&call(tool, &[], &[])).expect_err("refused");
            assert_eq!(error.kind, ErrorKind::Invalid, "{tool}");
        }
    }

    #[test]
    fn path_values_must_match_the_route_and_argument_names_must_be_plain() {
        assert!(route_of(&call("get_decision", &[], &[])).is_err());
        assert!(route_of(&call("search", &[("item_id", "x")], &[])).is_err());
        assert!(route_of(&call("search", &[], &[("que ry", "x")])).is_err());
        assert!(route_of(&call("search", &[], &[("", "x")])).is_err());
        assert!(route_of(&call("search", &[], &[("a&b=c", "x")])).is_err());
        let long = "x".repeat(MAX_VALUE_LENGTH + 1);
        assert!(route_of(&call("search", &[], &[("query", &long)])).is_err());
    }
}
