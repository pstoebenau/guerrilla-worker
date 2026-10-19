use crate::{api::Client, protocol::Fence};
use anyhow::{bail, Result};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use tokio::sync::{watch, Mutex, Notify};
use tokio_tungstenite::tungstenite::{client::IntoClientRequest, Message};
use tokio_util::sync::CancellationToken;

#[derive(Clone)]
struct Active {
    fence: Fence,
    cancel: CancellationToken,
    cancelled: Arc<AtomicBool>,
    drain: Arc<AtomicBool>,
}
/// The socket is only a notification transport. D1 reconciliation decides whether
/// an active attempt may publish. Losing transport never cancels computation.
pub struct Session {
    ready: watch::Sender<bool>,
    changed: watch::Sender<u64>,
    active: Mutex<Option<Active>>,
    reconnect: Notify,
    finished: std::sync::Mutex<Option<(Fence, String)>>,
}
impl Session {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            ready: watch::channel(false).0,
            changed: watch::channel(0).0,
            active: Mutex::new(None),
            reconnect: Notify::new(),
            finished: std::sync::Mutex::new(None),
        })
    }
    pub fn changes(&self) -> watch::Receiver<u64> {
        self.changed.subscribe()
    }
    pub fn connected(&self) -> bool {
        *self.ready.borrow()
    }
    pub fn disconnect(&self) {
        self.ready.send_replace(false);
        self.reconnect.notify_one();
    }
    pub async fn activate(
        &self,
        fence: Fence,
        cancel: CancellationToken,
        cancelled: Arc<AtomicBool>,
        drain: Arc<AtomicBool>,
    ) {
        *self.finished.lock().unwrap() = None;
        *self.active.lock().await = Some(Active {
            fence,
            cancel,
            cancelled,
            drain,
        });
        self.disconnect();
    }
    pub fn confirmed(&self, fence: &Fence, state: &str) -> bool {
        self.finished
            .lock()
            .unwrap()
            .as_ref()
            .is_some_and(|(saved, finished)| saved == fence && finished == state)
    }
    pub fn connectivity(&self) -> watch::Receiver<bool> {
        self.ready.subscribe()
    }
    pub async fn clear(&self) {
        *self.active.lock().await = None;
    }
    pub async fn wait(&self, cancel: &CancellationToken) -> Result<()> {
        let mut ready = self.ready.subscribe();
        loop {
            if *ready.borrow_and_update() {
                return Ok(());
            }
            tokio::select! {
                _ = cancel.cancelled() => bail!("Worker operation interrupted"),
                r = ready.changed() => { r?; }
            }
        }
    }
    async fn reconcile(&self, client: &Client, shutdown: &CancellationToken) -> Result<()> {
        // Keep activation serialized with the check and publication gate.
        let active = self.active.lock().await;
        if let Some(active) = active.as_ref() {
            let response: Value = client
                .post("reconcile", &serde_json::to_value(&active.fence)?, shutdown)
                .await?;
            match response["command"].as_str() {
                Some("continue") => {}
                Some("drain") => {
                    active.drain.store(true, Ordering::SeqCst);
                }
                Some("stop") => {
                    if let Some(state @ ("completed" | "failed" | "interrupted")) =
                        response["state"].as_str()
                    {
                        *self.finished.lock().unwrap() = Some((active.fence.clone(), state.into()));
                    }
                    active
                        .cancelled
                        .store(response["reason"] == "cancelled", Ordering::SeqCst);
                    active.cancel.cancel();
                }
                _ => bail!("Invalid worker reconciliation response"),
            }
        }
        self.ready.send_replace(true);
        Ok(())
    }
    pub async fn run(self: Arc<Self>, client: Client, shutdown: CancellationToken) {
        let mut failures = 0u32;
        while !shutdown.is_cancelled() {
            // A notification left from the previous failed request must not tear
            // down a freshly reconciled socket.
            let reconnect = self.reconnect.notified();
            tokio::pin!(reconnect);
            reconnect.as_mut().enable();
            let attempt = async {
                let url = format!("{}/api/worker/connect", client.origin)
                    .replacen("https:", "wss:", 1)
                    .replacen("http:", "ws:", 1);
                let mut request = url.into_client_request()?;
                request.headers_mut().insert(
                    "authorization",
                    format!("Bearer {}", client.credential()).parse()?,
                );
                let (mut socket, _) = tokio::time::timeout(
                    std::time::Duration::from_secs(30),
                    tokio_tungstenite::connect_async(request),
                )
                .await??;
                let tcp = match socket.get_ref() {
                    tokio_tungstenite::MaybeTlsStream::Plain(tcp) => tcp,
                    tokio_tungstenite::MaybeTlsStream::Rustls(tls) => tls.get_ref().0,
                    _ => bail!("Unsupported worker socket transport"),
                };
                // OS-level probes detect half-open idle connections without
                // application frames or waking the control-plane Durable Object.
                socket2::SockRef::from(tcp).set_tcp_keepalive(
                    &socket2::TcpKeepalive::new()
                        .with_time(std::time::Duration::from_secs(60))
                        .with_interval(std::time::Duration::from_secs(30))
                        .with_retries(3),
                )?;
                self.reconcile(&client, &shutdown).await?;
                self.changed.send_modify(|n| *n = n.wrapping_add(1));
                loop {
                    tokio::select! {
                        _ = shutdown.cancelled() => return Ok::<(), anyhow::Error>(()),
                        _ = &mut reconnect => bail!("Connection requires reconciliation"),
                        frame = socket.next() => {
                            let Some(frame) = frame else { bail!("Worker connection closed"); };
                            match frame? {
                                Message::Text(text) => {
                                    let value: Value = serde_json::from_str(&text)?;
                                    match value["type"].as_str() {
                                        Some("status") => {
                                            let active = self.active.lock().await;
                                            let mut reply = active.as_ref().map(|a| serde_json::to_value(&a.fence).unwrap()).unwrap_or_else(|| json!({}));
                                            reply["type"] = json!("status");
                                            reply["requestId"] = value["requestId"].clone();
                                            reply["state"] = json!(if active.is_some() { "busy" } else { "idle" });
                                            socket.send(Message::Text(reply.to_string().into())).await?;
                                        },
                                        Some("work_available" | "cancel" | "command") => {
                                            self.ready.send_replace(false);
                                            self.reconcile(&client, &shutdown).await?;
                                            self.changed.send_modify(|n| *n = n.wrapping_add(1));
                                        },
                                        Some("ready") => {},
                                        _ => bail!("Invalid worker connection message"),
                                    }
                                },
                                Message::Ping(bytes) => socket.send(Message::Pong(bytes)).await?,
                                Message::Close(_) => bail!("Worker connection closed"),
                                _ => {},
                            }
                        }
                    }
                }
            };
            let connected_at = std::time::Instant::now();
            let failure = tokio::select! { _ = shutdown.cancelled() => break, result = attempt => result.err() };
            let revoked = failure.as_ref().is_some_and(|error| {
                error.downcast_ref::<crate::api::ApiError>().is_some_and(|e| matches!(e.0, 401 | 403)) ||
                error.downcast_ref::<tokio_tungstenite::tungstenite::Error>().is_some_and(|e| {
                    matches!(e, tokio_tungstenite::tungstenite::Error::Http(response) if matches!(response.status().as_u16(), 401 | 403))
                })
            });
            if revoked {
                if let Some(active) = self.active.lock().await.as_ref() {
                    active.cancel.cancel();
                }
                eprintln!("Worker authorization was revoked; stopped execution and retained local outputs");
                shutdown.cancel();
                break;
            }
            self.ready.send_replace(false);
            self.changed.send_modify(|n| *n = n.wrapping_add(1));
            if connected_at.elapsed() >= std::time::Duration::from_secs(60) {
                failures = 0;
            }
            let seconds = (1u64 << failures.min(6)).min(60);
            failures = failures.saturating_add(1);
            tokio::select! { _ = shutdown.cancelled() => break, _ = tokio::time::sleep(std::time::Duration::from_secs(seconds)) => {} }
        }
        self.ready.send_replace(false);
    }
}
