//! A stand-in server for tests: it answers each request from a function and keeps what it was
//! asked. It listens on this computer only and lives as long as the test process.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};

/// A request as the stand-in saw it.
pub struct Request {
    /// The method and the path: `GET /v1/status`.
    pub line: String,
    pub body: String,
    /// Whether a bearer token came with it.
    pub with_token: bool,
}

pub struct Stub {
    /// The base address: `http://127.0.0.1:<port>`.
    pub address: String,
    requests: Arc<Mutex<Vec<String>>>,
}

impl Stub {
    /// Starts a server that answers every request with the status and the JSON `answer` gives.
    pub fn start(answer: impl Fn(&Request) -> (u16, String) + Send + 'static) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bound");
        let address = format!("http://{}", listener.local_addr().expect("address"));
        let requests = Arc::new(Mutex::new(Vec::new()));
        let seen = Arc::clone(&requests);
        std::thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let request = serve(stream, &answer);
                seen.lock()
                    .expect("lock")
                    .push(format!("{} {}", request.line, request.body));
            }
        });
        Self { address, requests }
    }

    /// Every request so far: its line, a space and its body.
    pub fn asked(&self) -> Vec<String> {
        self.requests.lock().expect("lock").clone()
    }

    /// An address on this computer that nothing listens at.
    pub fn silent_address() -> String {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bound");
        format!("http://{}", listener.local_addr().expect("address"))
    }
}

/// The error body of the Memini API.
pub fn api_error(code: &str, message: &str) -> String {
    format!("{{\"error\":{{\"code\":\"{code}\",\"message\":\"{message}\",\"request_id\":\"r1\"}}}}")
}

fn read(stream: &TcpStream) -> Request {
    let mut reader = BufReader::new(stream.try_clone().expect("cloned"));
    let mut first = String::new();
    reader.read_line(&mut first).expect("read");
    let (mut length, mut with_token) = (0, false);
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).expect("read");
        let header = line.to_ascii_lowercase();
        if let Some(value) = header.strip_prefix("content-length:") {
            length = value.trim().parse().expect("a length");
        }
        with_token |= header.starts_with("authorization: bearer ");
        if line.trim().is_empty() {
            break;
        }
    }
    let mut body = vec![0; length];
    reader.read_exact(&mut body).expect("read");
    Request {
        line: first.trim_end().trim_end_matches(" HTTP/1.1").to_owned(),
        body: String::from_utf8_lossy(&body).into_owned(),
        with_token,
    }
}

fn serve(mut stream: TcpStream, answer: &impl Fn(&Request) -> (u16, String)) -> Request {
    let request = read(&stream);
    let (status, json) = answer(&request);
    write!(
        stream,
        "HTTP/1.1 {status} Answer\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{json}",
        json.len()
    )
    .expect("written");
    request
}
