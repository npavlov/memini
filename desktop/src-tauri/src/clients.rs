//! Agent clients on this computer, and the one entry the app keeps in each client's own
//! configuration: the MCP server named `memini`.
//!
//! The clients are a fixed table, those the MCP guide documents (`docs/mcp`). For each the module
//! knows where its configuration is, what the entry looks like there and how it refers to a
//! token. It only ever adds or removes the entry named `memini`: every other byte of a JSON file
//! stays, a file it cannot read as that client's format is refused, and a change is checked
//! against the value of the file before it is written. Nothing here is run, and what a file says
//! is data: it is shown, never followed.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::Serialize;
use serde_json::{Map, Value};
use toml_edit::{DocumentMut, Item, Table};

use crate::error::{clean, AppError, AppResult, ErrorKind};
use crate::jsonc::{self, Document, Layout, Object};
use crate::secret::{redact, Token};

/// The name of the entry. The app touches no other.
pub const ENTRY: &str = "memini";
/// What is added to a file's name for the copy made before the app first changes it.
pub const BACKUP_SUFFIX: &str = ".memini-backup";
const TEMPORARY_SUFFIX: &str = ".memini-tmp";
/// The largest configuration file that is read, in bytes.
const MAX_FILE: u64 = 8 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    ClaudeCode,
    Codex,
    Cursor,
    VsCodeCopilot,
    Gemini,
    Windsurf,
    Zed,
    Cline,
    OpenCode,
}

/// A client the app can connect.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Client {
    pub kind: Kind,
    pub id: &'static str,
    pub name: &'static str,
    /// The environment variable its entry refers to. Each client has its own, because each has
    /// its own token. `None`: the client takes only a token written into its file.
    pub variable: Option<&'static str>,
}

const fn client(
    kind: Kind,
    id: &'static str,
    name: &'static str,
    variable: &'static str,
) -> Client {
    Client {
        kind,
        id,
        name,
        variable: Some(variable),
    }
}

pub const CLIENTS: [Client; 9] = [
    client(
        Kind::ClaudeCode,
        "claude-code",
        "Claude Code",
        "MEMINI_READ_TOKEN_CLAUDE_CODE",
    ),
    client(Kind::Codex, "codex", "Codex CLI", "MEMINI_READ_TOKEN_CODEX"),
    client(Kind::Cursor, "cursor", "Cursor", "MEMINI_READ_TOKEN_CURSOR"),
    client(
        Kind::VsCodeCopilot,
        "vscode-copilot",
        "VS Code with GitHub Copilot",
        "MEMINI_READ_TOKEN_VSCODE_COPILOT",
    ),
    client(
        Kind::Gemini,
        "gemini",
        "Gemini CLI",
        "MEMINI_READ_TOKEN_GEMINI",
    ),
    client(
        Kind::Windsurf,
        "windsurf",
        "Windsurf",
        "MEMINI_READ_TOKEN_WINDSURF",
    ),
    Client {
        kind: Kind::Zed,
        id: "zed",
        name: "Zed",
        variable: None,
    },
    client(Kind::Cline, "cline", "Cline", "MEMINI_READ_TOKEN_CLINE"),
    client(
        Kind::OpenCode,
        "opencode",
        "OpenCode",
        "MEMINI_READ_TOKEN_OPENCODE",
    ),
];

pub fn find(id: &str) -> AppResult<&'static Client> {
    CLIENTS
        .iter()
        .find(|known| known.id == id)
        .ok_or_else(|| AppError::invalid("The app does not know that client."))
}

/// The directories the clients keep their configuration under.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Places {
    pub home: PathBuf,
    /// The configuration directory of the operating system: `~/Library/Application Support`,
    /// `~/.config` or `%APPDATA%`.
    pub config: PathBuf,
    pub windows: bool,
}

impl Places {
    /// The places of this computer; `None` when the home directory is not known.
    pub fn from_environment() -> Option<Self> {
        let variable = |name: &str| {
            std::env::var_os(name)
                .filter(|value| !value.is_empty())
                .map(PathBuf::from)
        };
        let home = variable(if cfg!(windows) { "USERPROFILE" } else { "HOME" })?;
        let config = if cfg!(windows) {
            variable("APPDATA")?
        } else if cfg!(target_os = "macos") {
            home.join("Library").join("Application Support")
        } else {
            variable("XDG_CONFIG_HOME").unwrap_or_else(|| home.join(".config"))
        };
        Some(Self {
            home,
            config,
            windows: cfg!(windows),
        })
    }

    fn under_home(&self, parts: &[&str]) -> PathBuf {
        parts
            .iter()
            .fold(self.home.clone(), |path, part| path.join(part))
    }

    fn under_config(&self, parts: &[&str]) -> PathBuf {
        parts
            .iter()
            .fold(self.config.clone(), |path, part| path.join(part))
    }

    /// Where the app keeps the token it made for a client, for the client's shell to load.
    pub fn token_file(&self, client: &Client) -> PathBuf {
        self.under_home(&[".config", "memini", "clients"])
            .join(format!("{}.token", client.id))
    }
}

/// A way a client's configuration can be laid out, when it has more than one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Variant {
    pub id: &'static str,
    pub label: &'static str,
}

const OPENCODE_ONE: &str = "1";
const OPENCODE_TWO: &str = "2";
const OPENCODE_VARIANTS: [Variant; 2] = [
    Variant {
        id: OPENCODE_ONE,
        label: "OpenCode 1.x",
    },
    Variant {
        id: OPENCODE_TWO,
        label: "OpenCode 2.x",
    },
];

impl Client {
    /// The directory, or for Claude Code also the file, that is there when the client is
    /// installed.
    fn marks(&self, places: &Places) -> Vec<PathBuf> {
        match self.kind {
            Kind::ClaudeCode => vec![
                places.under_home(&[".claude.json"]),
                places.under_home(&[".claude"]),
            ],
            Kind::Codex => vec![places.under_home(&[".codex"])],
            Kind::Cursor => vec![places.under_home(&[".cursor"])],
            Kind::VsCodeCopilot => vec![places.under_home(&[".copilot"])],
            Kind::Gemini => vec![places.under_home(&[".gemini"])],
            Kind::Windsurf => vec![places.under_home(&[".codeium", "windsurf"])],
            Kind::Zed => vec![self.zed_directory(places)],
            Kind::Cline => vec![self.cline_directory(places)],
            Kind::OpenCode => vec![places.under_home(&[".config", "opencode"])],
        }
    }

    fn zed_directory(&self, places: &Places) -> PathBuf {
        if places.windows {
            places.under_config(&["Zed"])
        } else {
            places.under_home(&[".config", "zed"])
        }
    }

    fn cline_directory(&self, places: &Places) -> PathBuf {
        places.under_config(&["Code", "User", "globalStorage", "saoudrizwan.claude-dev"])
    }

    pub fn installed(&self, places: &Places) -> bool {
        self.marks(places).iter().any(|mark| mark.exists())
    }

    /// The client's own configuration file for this user.
    pub fn file(&self, places: &Places) -> PathBuf {
        match self.kind {
            Kind::ClaudeCode => places.under_home(&[".claude.json"]),
            Kind::Codex => places.under_home(&[".codex", "config.toml"]),
            Kind::Cursor => places.under_home(&[".cursor", "mcp.json"]),
            Kind::VsCodeCopilot => places.under_home(&[".copilot", "mcp-config.json"]),
            Kind::Gemini => places.under_home(&[".gemini", "settings.json"]),
            Kind::Windsurf => places.under_home(&[".codeium", "windsurf", "mcp_config.json"]),
            Kind::Zed => self.zed_directory(places).join("settings.json"),
            Kind::Cline => self
                .cline_directory(places)
                .join("settings")
                .join("cline_mcp_settings.json"),
            Kind::OpenCode => {
                let directory = places.under_home(&[".config", "opencode"]);
                let with_comments = directory.join("opencode.jsonc");
                if with_comments.is_file() {
                    with_comments
                } else {
                    directory.join("opencode.json")
                }
            }
        }
    }

    /// How the client's entry refers to its environment variable; `None` when it cannot.
    pub fn reference(&self) -> Option<String> {
        let variable = self.variable?;
        Some(match self.kind {
            Kind::Cursor | Kind::Windsurf | Kind::Cline => format!("${{env:{variable}}}"),
            Kind::OpenCode => format!("{{env:{variable}}}"),
            Kind::Codex => variable.to_owned(),
            _ => format!("${{{variable}}}"),
        })
    }

    /// Whether the app can write the token itself into the client's file.
    pub const fn takes_literal(&self) -> bool {
        !matches!(self.kind, Kind::Codex)
    }

    pub fn variants(&self) -> &'static [Variant] {
        match self.kind {
            Kind::OpenCode => &OPENCODE_VARIANTS,
            _ => &[],
        }
    }

    /// The objects the entry is inside, from the root, in the layouts the client has.
    fn parents(&self, variant: Option<&str>) -> &'static [&'static str] {
        match (self.kind, variant) {
            (Kind::Zed, _) => &["context_servers"],
            (Kind::OpenCode, Some(OPENCODE_TWO)) => &["mcp", "servers"],
            (Kind::OpenCode, _) => &["mcp"],
            (Kind::Codex, _) => &["mcp_servers"],
            _ => &["mcpServers"],
        }
    }

    /// The entry as the client's documentation shows it. `bearer` is what follows `Bearer `: the
    /// client's reference to its variable, or a token.
    pub fn shape(&self, url: &str, bearer: &str) -> Shape {
        let text = |value: &str| Shape::Text(value.to_owned());
        let headers = Shape::Group(vec![(
            "Authorization".to_owned(),
            Shape::Text(format!("Bearer {bearer}")),
        )]);
        let members = match self.kind {
            Kind::ClaudeCode => vec![
                ("type", text("http")),
                ("url", text(url)),
                ("headers", headers),
            ],
            Kind::VsCodeCopilot => vec![
                ("type", text("http")),
                ("url", text(url)),
                ("headers", headers),
                ("tools", Shape::List(vec!["*".to_owned()])),
            ],
            Kind::Gemini => vec![("httpUrl", text(url)), ("headers", headers)],
            Kind::Windsurf => vec![("serverUrl", text(url)), ("headers", headers)],
            Kind::Cline => vec![
                ("type", text("streamableHttp")),
                ("url", text(url)),
                ("headers", headers),
            ],
            Kind::OpenCode => vec![
                ("type", text("remote")),
                ("url", text(url)),
                ("headers", headers),
                ("oauth", Shape::Flag(false)),
            ],
            Kind::Codex => vec![("url", text(url)), ("bearer_token_env_var", text(bearer))],
            Kind::Cursor | Kind::Zed => vec![("url", text(url)), ("headers", headers)],
        };
        Shape::Group(
            members
                .into_iter()
                .map(|(key, shape)| (key.to_owned(), shape))
                .collect(),
        )
    }
}

/// A value the app writes, with its members in the order the client's documentation has them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Shape {
    Text(String),
    Flag(bool),
    List(Vec<String>),
    Group(Vec<(String, Shape)>),
}

fn quoted(text: &str) -> String {
    Value::String(text.to_owned()).to_string()
}

impl Shape {
    pub fn value(&self) -> Value {
        match self {
            Self::Text(text) => Value::String(text.clone()),
            Self::Flag(flag) => Value::Bool(*flag),
            Self::List(items) => items.iter().cloned().map(Value::String).collect(),
            Self::Group(members) => Value::Object(
                members
                    .iter()
                    .map(|(key, shape)| (key.clone(), shape.value()))
                    .collect(),
            ),
        }
    }

    /// The value as JSON text that starts on a line with `indent`.
    fn render(&self, indent: &str, layout: &Layout) -> String {
        match self {
            Self::Text(text) => quoted(text),
            Self::Flag(flag) => flag.to_string(),
            Self::List(items) => {
                let items: Vec<String> = items.iter().map(|item| quoted(item)).collect();
                format!("[{}]", items.join(", "))
            }
            Self::Group(members) => {
                let newline = layout.newline;
                let inner = format!("{indent}{}", layout.unit);
                let lines: Vec<String> = members
                    .iter()
                    .map(|(key, shape)| {
                        format!("{inner}{}: {}", quoted(key), shape.render(&inner, layout))
                    })
                    .collect();
                format!(
                    "{{{newline}{}{newline}{indent}}}",
                    lines.join(&format!(",{newline}"))
                )
            }
        }
    }

    /// This value inside objects named by `keys`, the first outermost.
    fn inside(self, keys: &[&str]) -> Self {
        keys.iter().rev().fold(self, |shape, key| {
            Self::Group(vec![((*key).to_owned(), shape)])
        })
    }
}

/// How the token of an entry is given.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TokenForm {
    /// The entry names an environment variable.
    Reference,
    /// The token is written in the file.
    Literal,
    /// The entry has no token.
    None,
}

/// What a client's file says about its `memini` entry. Never the token.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub url: Option<String>,
    pub token: TokenForm,
}

/// How the token is passed in an entry the app writes.
#[derive(Debug, Clone, Copy)]
pub enum Pass<'a> {
    Reference,
    Literal(&'a str),
}

/// What Connect is asked to write.
#[derive(Debug, Clone, Copy)]
pub struct Request<'a> {
    pub url: &'a str,
    pub pass: Pass<'a>,
    pub variant: Option<&'a str>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Planned {
    /// The entry is there already: nothing is written.
    Unchanged,
    /// The whole new text of the file.
    Change(String),
}

fn malformed(client: &Client) -> AppError {
    AppError::invalid(format!(
        "The configuration file of {} is not in the format the client reads. The app left it as \
         it is. Correct the file and try again.",
        client.name
    ))
}

fn unsupported(client: &Client) -> AppError {
    AppError::invalid(format!(
        "The configuration file of {} holds its MCP servers in a form the app does not change. \
         The app left it as it is. Edit the entry by hand.",
        client.name
    ))
}

fn unsafe_change() -> AppError {
    AppError::new(
        ErrorKind::Unexpected,
        "The app could not make this change and keep the rest of the file, so it left the file \
         as it is.",
    )
}

fn bearer(client: &Client, pass: Pass<'_>) -> AppResult<String> {
    match pass {
        Pass::Reference => client.reference().ok_or_else(|| {
            AppError::invalid(format!(
                "{} takes only a token written into its file.",
                client.name
            ))
        }),
        Pass::Literal(token) if client.takes_literal() => Ok(token.to_owned()),
        Pass::Literal(_) => Err(AppError::invalid(format!(
            "The app does not write a token into the file of {}.",
            client.name
        ))),
    }
}

/// The layout of an OpenCode file: version 2 keeps its servers under `mcp.servers`, version 1
/// directly under `mcp`. `None` when the file does not tell.
fn opencode_variant(root: &Value) -> Option<&'static str> {
    let is_server = |value: &Value| value.get("type").is_some_and(Value::is_string);
    let servers = root.get("mcp")?.as_object()?;
    if servers
        .get("servers")
        .is_some_and(|inner| inner.is_object() && !is_server(inner))
    {
        return Some(OPENCODE_TWO);
    }
    servers.values().any(is_server).then_some(OPENCODE_ONE)
}

/// The variant a file is in, or the one asked for when the file does not tell.
pub fn variant_of(client: &Client, root: Option<&Value>) -> Option<&'static str> {
    match client.kind {
        Kind::OpenCode => root.and_then(opencode_variant),
        _ => None,
    }
}

fn chosen_variant(
    client: &Client,
    root: Option<&Value>,
    asked: Option<&str>,
) -> AppResult<Option<&'static str>> {
    if client.variants().is_empty() {
        return Ok(None);
    }
    let known = |id: &str| client.variants().iter().find(|variant| variant.id == id);
    variant_of(client, root)
        .or_else(|| asked.and_then(known).map(|variant| variant.id))
        .map(Some)
        .ok_or_else(|| {
            AppError::invalid(format!(
                "Choose the version of {} first: its file does not tell.",
                client.name
            ))
        })
}

enum Located<'a> {
    /// The object the entry belongs in.
    Parent(&'a Object),
    /// The deepest object that exists, and the keys still to make inside it.
    Missing(&'a Object, &'a [&'a str]),
}

fn locate<'a>(client: &Client, root: &'a Object, path: &'a [&'a str]) -> AppResult<Located<'a>> {
    let mut object = root;
    for (depth, key) in path.iter().enumerate() {
        let found = object.named(key);
        let Some((_, member)) = found.first() else {
            return Ok(Located::Missing(object, path.get(depth..).unwrap_or(&[])));
        };
        if found.len() > 1 {
            return Err(unsupported(client));
        }
        object = member.object.as_ref().ok_or_else(|| unsupported(client))?;
    }
    Ok(Located::Parent(object))
}

/// Where the entry is: its parent and its position there.
fn entry_at<'a>(
    client: &Client,
    document: &'a Document,
) -> AppResult<Option<(&'a Object, usize, &'static [&'static str])>> {
    let variants: Vec<Option<&str>> = if client.variants().is_empty() {
        vec![None]
    } else {
        client
            .variants()
            .iter()
            .rev()
            .map(|variant| Some(variant.id))
            .collect()
    };
    for variant in variants {
        let path = client.parents(variant);
        let Located::Parent(parent) = locate(client, &document.root, path)? else {
            continue;
        };
        let found = parent.named(ENTRY);
        if found.len() > 1 {
            return Err(unsupported(client));
        }
        if let Some((index, _)) = found.first() {
            return Ok(Some((parent, *index, path)));
        }
    }
    Ok(None)
}

fn value_at<'a>(root: &'a Value, path: &[&str]) -> Option<&'a Value> {
    path.iter().try_fold(root, |value, key| value.get(key))
}

/// `root` with `entry` under the keys of `path`, making the objects that are missing.
fn with_entry(mut root: Value, path: &[&str], entry: Value) -> Option<Value> {
    let mut object = root.as_object_mut()?;
    for key in path {
        object = object
            .entry((*key).to_owned())
            .or_insert_with(|| Value::Object(Map::new()))
            .as_object_mut()?;
    }
    object.insert(ENTRY.to_owned(), entry);
    Some(root)
}

fn without_entry(mut root: Value, path: &[&str]) -> Option<Value> {
    let mut object = root.as_object_mut()?;
    for key in path {
        object = object.get_mut(*key)?.as_object_mut()?;
    }
    object.remove(ENTRY)?;
    Some(root)
}

fn form_of(authorization: Option<&str>) -> TokenForm {
    match authorization.map(str::trim) {
        None | Some("" | "Bearer") => TokenForm::None,
        Some(text) if redact(text) != text => TokenForm::Literal,
        Some(text) if text.contains('$') || text.contains("{env:") => TokenForm::Reference,
        Some(_) => TokenForm::Literal,
    }
}

fn json_entry(entry: &Value) -> Entry {
    let url = ["url", "httpUrl", "serverUrl"]
        .iter()
        .find_map(|key| entry.get(key).and_then(Value::as_str))
        .map(clean);
    let authorization = entry
        .get("headers")
        .and_then(|headers| headers.get("Authorization"))
        .and_then(Value::as_str);
    Entry {
        url,
        token: form_of(authorization),
    }
}

/// A new file with the entry alone.
fn fresh_json(path: &[&str], shape: Shape) -> String {
    let layout = Layout {
        indent: String::new(),
        unit: "  ".to_owned(),
        newline: "\n",
    };
    let whole = shape.inside(&[ENTRY]).inside(path);
    format!("{}\n", whole.render("", &layout))
}

fn connect_json(client: &Client, text: Option<&str>, request: &Request<'_>) -> AppResult<Planned> {
    let bearer = bearer(client, request.pass)?;
    let shape = client.shape(request.url, &bearer);
    let Some(text) = text.filter(|text| !text.trim().is_empty()) else {
        let variant = chosen_variant(client, None, request.variant)?;
        return Ok(Planned::Change(fresh_json(client.parents(variant), shape)));
    };
    let document = jsonc::parse(text).map_err(|_| malformed(client))?;
    if entry_at(client, &document)?.is_some() {
        return Ok(Planned::Unchanged);
    }
    let variant = chosen_variant(client, Some(&document.value), request.variant)?;
    let path = client.parents(variant);
    let expected = with_entry(document.value.clone(), path, shape.value());
    let edit = match locate(client, &document.root, path)? {
        Located::Parent(parent) => {
            let layout = jsonc::layout(text, parent);
            let member = format!(
                "{}: {}",
                quoted(ENTRY),
                shape.render(&layout.indent, &layout)
            );
            jsonc::insert(text, parent, &layout, &member)
        }
        Located::Missing(object, missing) => {
            let layout = jsonc::layout(text, object);
            let (first, rest) = missing.split_first().ok_or_else(unsafe_change)?;
            let nested = shape.inside(&[ENTRY]).inside(rest);
            let member = format!(
                "{}: {}",
                quoted(first),
                nested.render(&layout.indent, &layout)
            );
            jsonc::insert(text, object, &layout, &member)
        }
    };
    let after = jsonc::apply(text, vec![edit]);
    checked_json(&after, expected).map(Planned::Change)
}

/// `after`, when it reads as JSON with exactly the value that was meant.
fn checked_json(after: &str, expected: Option<Value>) -> AppResult<String> {
    let read = jsonc::parse(after).map_err(|_| unsafe_change())?;
    if Some(read.value) != expected {
        return Err(unsafe_change());
    }
    Ok(after.to_owned())
}

fn disconnect_json(client: &Client, text: &str) -> AppResult<Option<String>> {
    if text.trim().is_empty() {
        return Ok(None);
    }
    let document = jsonc::parse(text).map_err(|_| malformed(client))?;
    let Some((parent, index, path)) = entry_at(client, &document)? else {
        return Ok(None);
    };
    let after = jsonc::apply(text, jsonc::remove(text, parent, index));
    let expected = without_entry(document.value.clone(), path);
    checked_json(&after, expected).map(Some)
}

fn read_json(client: &Client, text: &str) -> AppResult<Option<Entry>> {
    if text.trim().is_empty() {
        return Ok(None);
    }
    let document = jsonc::parse(text).map_err(|_| malformed(client))?;
    Ok(entry_at(client, &document)?
        .and_then(|(_, _, path)| value_at(&document.value, path))
        .and_then(|parent| parent.get(ENTRY))
        .map(json_entry))
}

fn neutral_value(value: &toml_edit::Value) -> Value {
    match value {
        toml_edit::Value::String(text) => Value::String(text.value().clone()),
        toml_edit::Value::Integer(number) => Value::from(*number.value()),
        toml_edit::Value::Float(number) => Value::from(*number.value()),
        toml_edit::Value::Boolean(flag) => Value::Bool(*flag.value()),
        toml_edit::Value::Datetime(time) => Value::String(time.value().to_string()),
        toml_edit::Value::Array(items) => items.iter().map(neutral_value).collect(),
        toml_edit::Value::InlineTable(table) => Value::Object(
            table
                .iter()
                .map(|(key, value)| (key.to_owned(), neutral_value(value)))
                .collect(),
        ),
    }
}

fn neutral_table(table: &Table) -> Value {
    Value::Object(
        table
            .iter()
            .map(|(key, item)| (key.to_owned(), neutral(item)))
            .collect(),
    )
}

/// A TOML item as a plain value, to compare what a file says before and after a change.
fn neutral(item: &Item) -> Value {
    match item {
        Item::None => Value::Null,
        Item::Value(value) => neutral_value(value),
        Item::Table(table) => neutral_table(table),
        Item::ArrayOfTables(tables) => tables.iter().map(neutral_table).collect(),
    }
}

fn parse_toml(client: &Client, text: &str) -> AppResult<DocumentMut> {
    text.parse::<DocumentMut>().map_err(|_| malformed(client))
}

fn toml_servers<'a>(client: &Client, document: &'a DocumentMut) -> AppResult<Option<&'a Table>> {
    match document.get("mcp_servers") {
        None => Ok(None),
        Some(Item::Table(table)) => Ok(Some(table)),
        Some(_) => Err(unsupported(client)),
    }
}

fn checked_toml(client: &Client, after: String, expected: Option<Value>) -> AppResult<String> {
    let read = parse_toml(client, &after).map_err(|_| unsafe_change())?;
    if Some(neutral(read.as_item())) != expected {
        return Err(unsafe_change());
    }
    Ok(after)
}

fn connect_toml(client: &Client, text: Option<&str>, request: &Request<'_>) -> AppResult<Planned> {
    let variable = bearer(client, request.pass)?;
    let mut document = parse_toml(client, text.unwrap_or(""))?;
    if toml_servers(client, &document)?.is_some_and(|servers| servers.contains_key(ENTRY)) {
        return Ok(Planned::Unchanged);
    }
    let shape = client.shape(request.url, &variable);
    let expected = with_entry(
        neutral(document.as_item()),
        client.parents(None),
        shape.value(),
    );
    if document.get("mcp_servers").is_none() {
        let mut servers = Table::new();
        servers.set_implicit(true);
        document.insert("mcp_servers", Item::Table(servers));
    }
    let servers = document
        .get_mut("mcp_servers")
        .and_then(Item::as_table_mut)
        .ok_or_else(|| unsupported(client))?;
    let mut entry = Table::new();
    entry.insert("url", toml_edit::value(request.url));
    entry.insert("bearer_token_env_var", toml_edit::value(variable));
    servers.insert(ENTRY, Item::Table(entry));
    checked_toml(client, document.to_string(), expected).map(Planned::Change)
}

fn disconnect_toml(client: &Client, text: &str) -> AppResult<Option<String>> {
    let mut document = parse_toml(client, text)?;
    if !toml_servers(client, &document)?.is_some_and(|servers| servers.contains_key(ENTRY)) {
        return Ok(None);
    }
    let expected = without_entry(neutral(document.as_item()), client.parents(None));
    let servers = document
        .get_mut("mcp_servers")
        .and_then(Item::as_table_mut)
        .ok_or_else(|| unsupported(client))?;
    servers.remove(ENTRY);
    checked_toml(client, document.to_string(), expected).map(Some)
}

fn read_toml(client: &Client, text: &str) -> AppResult<Option<Entry>> {
    let document = parse_toml(client, text)?;
    let Some(entry) = toml_servers(client, &document)?.and_then(|servers| servers.get(ENTRY))
    else {
        return Ok(None);
    };
    let entry = neutral(entry);
    let has_variable = entry
        .get("bearer_token_env_var")
        .is_some_and(Value::is_string);
    let written = json_entry(&entry);
    Ok(Some(Entry {
        url: written.url,
        token: if has_variable {
            TokenForm::Reference
        } else {
            written.token
        },
    }))
}

/// The text of the file with the entry added. An entry that is there already is left alone,
/// whatever it says: Connect twice changes nothing.
pub fn connect_text(
    client: &Client,
    text: Option<&str>,
    request: &Request<'_>,
) -> AppResult<Planned> {
    match client.kind {
        Kind::Codex => connect_toml(client, text, request),
        _ => connect_json(client, text, request),
    }
}

/// The text of the file without the entry; `None` when it has none.
pub fn disconnect_text(client: &Client, text: &str) -> AppResult<Option<String>> {
    match client.kind {
        Kind::Codex => disconnect_toml(client, text),
        _ => disconnect_json(client, text),
    }
}

/// What the file says about the entry; `None` when it has none.
pub fn read_entry(client: &Client, text: &str) -> AppResult<Option<Entry>> {
    match client.kind {
        Kind::Codex => read_toml(client, text),
        _ => read_json(client, text),
    }
}

/// The variant the file is laid out in, when it tells.
pub fn read_variant(client: &Client, text: &str) -> Option<&'static str> {
    let document = jsonc::parse(text).ok()?;
    variant_of(client, Some(&document.value))
}

fn storage(what: &str, error: &std::io::Error) -> AppError {
    // The kind only: the text of an input/output error can hold more than the user needs.
    AppError::new(ErrorKind::Storage, format!("{what} ({:?}).", error.kind()))
}

/// The text of a configuration file; `None` when there is no file.
pub fn read_file(path: &Path) -> AppResult<Option<String>> {
    let metadata = match fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(storage("The client's file cannot be read", &error)),
    };
    if !metadata.is_file() || metadata.len() > MAX_FILE {
        return Err(AppError::invalid(
            "The client's configuration is not a file the app can read.",
        ));
    }
    let bytes =
        fs::read(path).map_err(|error| storage("The client's file cannot be read", &error))?;
    String::from_utf8(bytes)
        .map(Some)
        .map_err(|_| AppError::invalid("The client's configuration file is not text in UTF-8."))
}

fn beside(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(suffix);
    path.with_file_name(name)
}

/// The copy made before the app first changes a file.
pub fn backup_of(path: &Path) -> PathBuf {
    beside(path, BACKUP_SUFFIX)
}

fn create(path: &Path, private: bool) -> std::io::Result<fs::File> {
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    if private {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    #[cfg(not(unix))]
    let _ = private;
    options.open(path)
}

/// Writes `text` beside `target` and moves it over. An existing file keeps its permissions,
/// unless the text holds a token: then only the user may read it.
fn replace(target: &Path, text: &str, private: bool) -> std::io::Result<()> {
    let temporary = beside(target, TEMPORARY_SUFFIX);
    let _ = fs::remove_file(&temporary);
    let written = create(&temporary, private).and_then(|mut file| {
        file.write_all(text.as_bytes())?;
        if !private {
            if let Ok(metadata) = fs::metadata(target) {
                fs::set_permissions(&temporary, metadata.permissions())?;
            }
        }
        file.sync_all()
    });
    let moved = written.and_then(|()| fs::rename(&temporary, target));
    if moved.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    moved
}

/// Writes a client's configuration file. A file that exists is copied first, once: the copy made
/// before the first change is never overwritten.
pub fn write_file(path: &Path, text: &str, private: bool) -> AppResult<()> {
    let failed = |error: std::io::Error| {
        storage(
            "The client's file could not be written, and was left as it is",
            &error,
        )
    };
    // A file that is a link is changed where it really is, and stays a link.
    let target = fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
    if let Some(directory) = target.parent() {
        fs::create_dir_all(directory).map_err(failed)?;
    }
    let backup = backup_of(&target);
    if target.is_file() && !backup.exists() {
        fs::copy(&target, &backup).map_err(failed)?;
    }
    replace(&target, text, private).map_err(failed)
}

/// Keeps the token made for a client in a file only the user can read.
pub fn save_token(path: &Path, token: &Token) -> AppResult<()> {
    let failed = |error: std::io::Error| storage("The token file could not be written", &error);
    if let Some(directory) = path.parent() {
        fs::create_dir_all(directory).map_err(failed)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(directory, fs::Permissions::from_mode(0o700)).map_err(failed)?;
        }
    }
    replace(path, &format!("{}\n", token.expose()), true).map_err(failed)
}

/// What the webview gets of a client.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ClientView {
    pub id: &'static str,
    pub name: &'static str,
    /// The client's configuration file.
    pub file: String,
    #[serde(flatten)]
    pub state: ClientState,
    /// The environment variable the client's entry can refer to.
    pub variable: Option<&'static str>,
    /// Whether the app can write the token itself into the client's file.
    pub takes_literal: bool,
    /// Where the app keeps the token it made for the client.
    pub token_file: String,
    pub has_token_file: bool,
    pub variants: &'static [Variant],
    /// The variant the file is in, when it tells.
    pub variant: Option<&'static str>,
    /// Whether the app has a token on record that it made for this client.
    pub has_token: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum ClientState {
    Absent,
    Connected {
        url: Option<String>,
        token: TokenForm,
    },
    /// The file cannot be read as the client's format.
    Unreadable {
        message: String,
    },
}

/// The client as its own file shows it now. `has_token`: whether the app remembers a token.
pub fn view(client: &'static Client, places: &Places, has_token: bool) -> ClientView {
    let file = client.file(places);
    let text = read_file(&file);
    let state = match &text {
        Err(error) => ClientState::Unreadable {
            message: error.message.clone(),
        },
        Ok(None) => ClientState::Absent,
        Ok(Some(text)) => match read_entry(client, text) {
            Err(error) => ClientState::Unreadable {
                message: error.message,
            },
            Ok(None) => ClientState::Absent,
            Ok(Some(entry)) => ClientState::Connected {
                url: entry.url,
                token: entry.token,
            },
        },
    };
    let token_file = places.token_file(client);
    ClientView {
        id: client.id,
        name: client.name,
        file: file.to_string_lossy().into_owned(),
        state,
        variable: client.variable,
        takes_literal: client.takes_literal(),
        has_token_file: token_file.is_file(),
        token_file: token_file.to_string_lossy().into_owned(),
        variants: client.variants(),
        variant: text
            .ok()
            .flatten()
            .and_then(|text| read_variant(client, &text)),
        has_token,
    }
}

/// The clients installed on this computer, in the order of the guide.
pub fn installed(places: &Places) -> Vec<&'static Client> {
    CLIENTS
        .iter()
        .filter(|client| client.installed(places))
        .collect()
}

#[cfg(test)]
pub mod fixtures {
    //! A copy of each client's configuration, written by hand, with a server of its own in it.

    use super::{find, Client};

    pub const URL: &str = "http://127.0.0.1:6364/mcp";

    pub struct Fixture {
        pub client: &'static Client,
        pub text: &'static str,
        pub variant: Option<&'static str>,
    }

    fn fixture(id: &str, text: &'static str, variant: Option<&'static str>) -> Fixture {
        Fixture {
            client: find(id).expect("a known client"),
            text,
            variant,
        }
    }

    pub fn all() -> Vec<Fixture> {
        vec![
            fixture(
                "claude-code",
                include_str!("../fixtures/clients/claude-code.json"),
                None,
            ),
            fixture(
                "codex",
                include_str!("../fixtures/clients/codex.toml"),
                None,
            ),
            fixture(
                "cursor",
                include_str!("../fixtures/clients/cursor.json"),
                None,
            ),
            fixture(
                "vscode-copilot",
                include_str!("../fixtures/clients/vscode-copilot.json"),
                None,
            ),
            fixture(
                "gemini",
                include_str!("../fixtures/clients/gemini.json"),
                None,
            ),
            fixture(
                "windsurf",
                include_str!("../fixtures/clients/windsurf.json"),
                None,
            ),
            fixture("zed", include_str!("../fixtures/clients/zed.jsonc"), None),
            fixture(
                "cline",
                include_str!("../fixtures/clients/cline.json"),
                None,
            ),
            fixture(
                "opencode",
                include_str!("../fixtures/clients/opencode-1.jsonc"),
                Some("1"),
            ),
            fixture(
                "opencode",
                include_str!("../fixtures/clients/opencode-2.jsonc"),
                Some("2"),
            ),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::fixtures::{all, Fixture, URL};
    use super::*;
    use crate::secret::fake_token;

    const GUIDE: &str = include_str!("../../../docs/mcp/README.md");
    const EDITORS: &str = include_str!("../../../docs/mcp/clients/editors.md");
    const MORE: &str = include_str!("../../../docs/mcp/clients/more.md");
    const TERMINAL: &str = include_str!("../../../docs/mcp/clients/terminal.md");
    const DOCUMENTED_VARIABLE: &str = "MEMINI_READ_TOKEN";
    const DOCUMENTED_TOKEN: &str = "<workspace-read-token>";

    /// The way the app passes the token to a client by default: its variable where it has one.
    fn pass_of<'a>(client: &Client, token: &'a str) -> Pass<'a> {
        if client.variable.is_some() {
            Pass::Reference
        } else {
            Pass::Literal(token)
        }
    }

    fn connected(fixture: &Fixture, token: &str) -> String {
        let request = Request {
            url: URL,
            pass: pass_of(fixture.client, token),
            variant: None,
        };
        match connect_text(fixture.client, Some(fixture.text), &request).expect("planned") {
            Planned::Change(text) => text,
            Planned::Unchanged => panic!("{} has no entry yet", fixture.client.id),
        }
    }

    /// The value of a configuration text, whatever its format.
    fn value_of(client: &Client, text: &str) -> Value {
        match client.kind {
            Kind::Codex => neutral(text.parse::<DocumentMut>().expect("toml").as_item()),
            _ => jsonc::parse(text).expect("json").value,
        }
    }

    #[test]
    fn connect_adds_the_entry_and_keeps_the_rest_of_every_clients_file() {
        let token = fake_token('a');

        let kept: Vec<bool> = all()
            .iter()
            .map(|fixture| {
                let after = value_of(fixture.client, &connected(fixture, &token));
                let path = fixture.client.parents(fixture.variant);
                let before = value_of(fixture.client, fixture.text);
                without_entry(after, path) == Some(before)
            })
            .collect();

        assert_eq!(kept, vec![true; 10]);
    }

    #[test]
    fn the_entry_connect_writes_is_the_one_the_file_then_shows() {
        let token = fake_token('a');

        let shown: Vec<Option<Entry>> = all()
            .iter()
            .map(|fixture| {
                read_entry(fixture.client, &connected(fixture, &token)).expect("readable")
            })
            .collect();

        let expected = |fixture: &Fixture| {
            Some(Entry {
                url: Some(URL.to_owned()),
                token: if fixture.client.variable.is_some() {
                    TokenForm::Reference
                } else {
                    TokenForm::Literal
                },
            })
        };
        assert_eq!(shown, all().iter().map(expected).collect::<Vec<_>>());
    }

    #[test]
    fn connect_twice_changes_nothing() {
        let token = fake_token('a');

        let second: Vec<Planned> = all()
            .iter()
            .map(|fixture| {
                let once = connected(fixture, &token);
                let request = Request {
                    url: "https://other.example/mcp",
                    pass: pass_of(fixture.client, &token),
                    variant: None,
                };
                connect_text(fixture.client, Some(&once), &request).expect("planned")
            })
            .collect();

        assert_eq!(second, vec![Planned::Unchanged; 10]);
    }

    #[test]
    fn disconnect_gives_every_clients_file_back_byte_for_byte() {
        let token = fake_token('a');

        let restored: Vec<Option<String>> = all()
            .iter()
            .map(|fixture| {
                disconnect_text(fixture.client, &connected(fixture, &token)).expect("planned")
            })
            .collect();

        let originals: Vec<Option<String>> = all()
            .iter()
            .map(|fixture| Some(fixture.text.to_owned()))
            .collect();
        assert_eq!(restored, originals);
    }

    #[test]
    fn disconnect_of_a_file_without_the_entry_changes_nothing() {
        let untouched: Vec<Option<String>> = all()
            .iter()
            .map(|fixture| disconnect_text(fixture.client, fixture.text).expect("planned"))
            .collect();

        assert_eq!(untouched, vec![None; 10]);
    }

    #[test]
    fn a_malformed_configuration_is_refused_by_connect_and_by_disconnect() {
        let token = fake_token('a');

        let refused: Vec<(bool, bool)> = all()
            .iter()
            .map(|fixture| {
                let broken = format!("{}\n}} = [", fixture.text);
                let request = Request {
                    url: URL,
                    pass: pass_of(fixture.client, &token),
                    variant: fixture.variant,
                };
                (
                    connect_text(fixture.client, Some(&broken), &request).is_err(),
                    disconnect_text(fixture.client, &broken).is_err(),
                )
            })
            .collect();

        assert_eq!(refused, vec![(true, true); 10]);
    }

    #[test]
    fn a_refusal_never_repeats_what_the_file_holds() {
        let token = fake_token('a');
        let broken = format!("{{\"mcpServers\": {{\"secret\": \"{token}\" ");
        let client = find("cursor").expect("known");

        let error = read_entry(client, &broken).expect_err("refused");

        assert!(!error.message.contains(&token) && !error.message.contains("secret"));
    }

    #[test]
    fn a_missing_or_empty_file_gets_the_entry_alone() {
        let client = find("cursor").expect("known");
        let request = Request {
            url: URL,
            pass: Pass::Reference,
            variant: None,
        };

        let fresh = connect_text(client, None, &request).expect("planned");
        let empty = connect_text(client, Some("  \n"), &request).expect("planned");

        let expected = "{\n  \"mcpServers\": {\n    \"memini\": {\n      \"url\": \"http://127.0.0.1:6364/mcp\",\n      \"headers\": {\n        \"Authorization\": \"Bearer ${env:MEMINI_READ_TOKEN_CURSOR}\"\n      }\n    }\n  }\n}\n";
        assert_eq!(
            (fresh, empty),
            (
                Planned::Change(expected.to_owned()),
                Planned::Change(expected.to_owned())
            )
        );
    }

    #[test]
    fn a_file_without_servers_gets_the_group_and_the_entry() {
        let client = find("gemini").expect("known");
        let text = "{\n    \"theme\": \"Default\"\n}\n";
        let request = Request {
            url: URL,
            pass: Pass::Reference,
            variant: None,
        };

        let planned = connect_text(client, Some(text), &request).expect("planned");

        let expected = "{\n    \"theme\": \"Default\",\n    \"mcpServers\": {\n        \"memini\": {\n            \"httpUrl\": \"http://127.0.0.1:6364/mcp\",\n            \"headers\": {\n                \"Authorization\": \"Bearer ${MEMINI_READ_TOKEN_GEMINI}\"\n            }\n        }\n    }\n}\n";
        assert_eq!(planned, Planned::Change(expected.to_owned()));
    }

    #[test]
    fn servers_that_are_not_an_object_are_left_alone() {
        let client = find("cursor").expect("known");
        let request = Request {
            url: URL,
            pass: Pass::Reference,
            variant: None,
        };

        let answer = connect_text(client, Some("{\"mcpServers\": []}"), &request);

        assert!(answer.is_err());
    }

    #[test]
    fn a_new_opencode_file_needs_its_version_chosen() {
        let client = find("opencode").expect("known");
        let request = |variant| Request {
            url: URL,
            pass: Pass::Reference,
            variant,
        };

        let unknown = connect_text(client, Some("{}"), &request(None));
        let second = connect_text(client, Some("{}"), &request(Some("2"))).expect("planned");

        let Planned::Change(text) = second else {
            panic!("an entry is written")
        };
        assert!(unknown.is_err() && text.contains("\"servers\": {"));
    }

    #[test]
    fn codex_gets_no_token_written_into_its_file() {
        let client = find("codex").expect("known");
        let token = fake_token('a');
        let request = Request {
            url: URL,
            pass: Pass::Literal(&token),
            variant: None,
        };

        let answer = connect_text(client, None, &request);

        assert!(answer.is_err());
    }

    #[test]
    fn a_token_in_a_file_is_reported_and_never_shown() {
        let token = fake_token('a');
        let client = find("zed").expect("known");
        let text = format!(
            "{{\"context_servers\": {{\"memini\": {{\"url\": \"{URL}\", \"headers\": {{\"Authorization\": \"Bearer {token}\"}}}}}}}}"
        );

        let entry = read_entry(client, &text).expect("readable");

        assert_eq!(
            entry,
            Some(Entry {
                url: Some(URL.to_owned()),
                token: TokenForm::Literal
            })
        );
    }

    /// The JSON example under a heading of the guide: the `nth` one, as a value.
    fn documented(page: &str, heading: &str, nth: usize) -> Value {
        let section = page.split(heading).nth(1).expect("the heading");
        let block = section.split("```json\n").nth(nth + 1).expect("an example");
        let example = block.split("```").next().expect("its end");
        serde_json::from_str(example).expect("json")
    }

    /// The entry the guide documents for a client, with the app's variable or token in it.
    fn documented_entry(fixture: &Fixture, token: &str) -> Value {
        let (page, heading, nth) = match (fixture.client.kind, fixture.variant) {
            (Kind::ClaudeCode, _) => (GUIDE, "\n## Claude Code\n", 0),
            (Kind::Cursor, _) => (EDITORS, "\n## Cursor\n", 0),
            (Kind::VsCodeCopilot, _) => (EDITORS, "\n## VS Code with GitHub Copilot\n", 0),
            (Kind::Gemini, _) => (EDITORS, "\n## Gemini CLI\n", 0),
            (Kind::Windsurf, _) => (MORE, "\n## Windsurf\n", 0),
            (Kind::Zed, _) => (MORE, "\n## Zed\n", 0),
            (Kind::Cline, _) => (MORE, "\n## Cline\n", 0),
            (Kind::OpenCode, Some("2")) => (TERMINAL, "\n## OpenCode\n", 1),
            (Kind::OpenCode, _) => (TERMINAL, "\n## OpenCode\n", 0),
            (Kind::Codex, _) => panic!("Codex is documented as a command"),
        };
        let own = fixture.client.variable.unwrap_or(DOCUMENTED_VARIABLE);
        let page = page
            .replace(DOCUMENTED_VARIABLE, own)
            .replace(DOCUMENTED_TOKEN, token);
        let example = documented(&page, heading, nth);
        value_at(&example, fixture.client.parents(fixture.variant))
            .and_then(|parent| parent.get(ENTRY))
            .cloned()
            .expect("the documented entry")
    }

    #[test]
    fn every_written_entry_is_the_one_the_guide_documents() {
        let token = fake_token('a');
        let fixtures: Vec<Fixture> = all()
            .into_iter()
            .filter(|fixture| fixture.client.kind != Kind::Codex)
            .collect();

        let written: Vec<Option<Value>> = fixtures
            .iter()
            .map(|fixture| {
                let after = value_of(fixture.client, &connected(fixture, &token));
                value_at(&after, fixture.client.parents(fixture.variant))
                    .and_then(|parent| parent.get(ENTRY))
                    .cloned()
            })
            .collect();

        let guide: Vec<Option<Value>> = fixtures
            .iter()
            .map(|fixture| Some(documented_entry(fixture, &token)))
            .collect();
        assert_eq!(written, guide);
    }

    #[test]
    fn the_codex_entry_is_what_the_documented_command_registers() {
        let command = GUIDE
            .lines()
            .find(|line| line.starts_with("codex mcp add memini "))
            .expect("the command");
        let words: Vec<&str> = command.split(' ').collect();
        let after = |flag: &str| {
            let at = words
                .iter()
                .position(|word| *word == flag)
                .expect("the flag");
            (*words.get(at + 1).expect("its value")).to_owned()
        };
        let client = find("codex").expect("known");
        let request = Request {
            url: &after("--url"),
            pass: Pass::Reference,
            variant: None,
        };

        let planned = connect_text(client, None, &request).expect("planned");

        let expected = format!(
            "[mcp_servers.memini]\nurl = \"{URL}\"\nbearer_token_env_var = \"{}_CODEX\"\n",
            after("--bearer-token-env-var")
        );
        assert_eq!(planned, Planned::Change(expected));
    }

    #[test]
    fn a_client_is_offered_only_when_its_directory_is_there() {
        let home =
            std::env::temp_dir().join(format!("memini-clients-found-{}", std::process::id()));
        let _ = fs::remove_dir_all(&home);
        fs::create_dir_all(home.join(".cursor")).expect("made");
        fs::create_dir_all(home.join(".config").join("zed")).expect("made");
        let places = Places {
            home: home.clone(),
            config: home.join("config"),
            windows: false,
        };

        let found: Vec<&str> = installed(&places).iter().map(|client| client.id).collect();

        let _ = fs::remove_dir_all(&home);
        assert_eq!(found, vec!["cursor", "zed"]);
    }

    #[test]
    fn every_client_keeps_its_configuration_under_the_users_own_directories() {
        let places = Places {
            home: PathBuf::from("/home/dana"),
            config: PathBuf::from("/home/dana/.config"),
            windows: false,
        };

        let outside = CLIENTS
            .iter()
            .filter(|client| !client.file(&places).starts_with("/home/dana"))
            .count();

        assert_eq!(outside, 0);
    }
}
