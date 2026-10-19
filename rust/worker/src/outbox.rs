use crate::{
    api::{ApiError, Client},
    files,
};
use anyhow::{bail, Result};
use serde_json::Value;
use std::{path::PathBuf, sync::Arc};
use tokio::sync::{Mutex, Notify};
use tokio_util::sync::CancellationToken;

#[derive(serde::Serialize, serde::Deserialize)]
struct Queue {
    events: Vec<Value>,
    sequence: u64,
    #[serde(skip)]
    error: Option<String>,
}
/// Only progress is replaceable; lifecycle and terminal events remain durable
/// until the server acknowledges their stable event identities.
pub struct Outbox {
    queue: Mutex<Queue>,
    path: PathBuf,
    wake: Notify,
    changed: Notify,
}
impl Outbox {
    pub fn open(path: PathBuf) -> Result<Arc<Self>> {
        let queue: Queue = match std::fs::read(&path) {
            Ok(bytes) => serde_json::from_slice(&bytes)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Queue {
                events: vec![],
                sequence: 0,
                error: None,
            },
            Err(error) => return Err(error.into()),
        };
        Ok(Arc::new(Self {
            queue: Mutex::new(queue),
            path,
            wake: Notify::new(),
            changed: Notify::new(),
        }))
    }
    pub async fn push(&self, mut event: Value) -> Result<()> {
        let mut queue = self.queue.lock().await;
        queue.sequence += 1;
        event["sequence"] = queue.sequence.into();
        let mut events = queue.events.clone();
        if matches!(event["kind"].as_str(), Some("progress" | "upload")) {
            events.retain(|previous| {
                previous["kind"] != event["kind"] || previous["stage"] != event["stage"]
            });
        }
        events.push(event);
        files::write_private(
            &self.path,
            &serde_json::to_vec(&Queue {
                events: events.clone(),
                sequence: queue.sequence,
                error: None,
            })?,
        )?;
        queue.events = events;
        drop(queue);
        self.wake.notify_one();
        Ok(())
    }
    pub async fn flush(&self, cancel: &CancellationToken) -> Result<()> {
        loop {
            let changed = self.changed.notified();
            let queue = self.queue.lock().await;
            if let Some(error) = &queue.error {
                bail!("Event publication failed: {error}");
            }
            if queue.events.is_empty() {
                return Ok(());
            }
            drop(queue);
            tokio::select! { _ = cancel.cancelled() => bail!("Event publication interrupted; local events retained"), _ = changed => {} }
        }
    }
    pub async fn run(self: Arc<Self>, client: Client, cancel: CancellationToken) {
        loop {
            let wake = self.wake.notified();
            let event = self.queue.lock().await.events.first().cloned();
            let Some(event) = event else {
                tokio::select! { _ = cancel.cancelled() => break, _ = wake => continue }
            };
            match client.post::<Value>("events", &event, &cancel).await {
                Ok(_) => {
                    let mut queue = self.queue.lock().await;
                    // An in-flight progress item may have been coalesced.
                    let mut events = queue.events.clone();
                    events.retain(|e| e["eventId"] != event["eventId"]);
                    match files::write_private(
                        &self.path,
                        &serde_json::to_vec(&Queue {
                            events: events.clone(),
                            sequence: queue.sequence,
                            error: None,
                        })
                        .unwrap(),
                    ) {
                        Ok(()) => queue.events = events,
                        Err(error) => {
                            queue.error = Some(error.to_string());
                            self.changed.notify_waiters();
                            break;
                        }
                    }
                    self.changed.notify_waiters();
                }
                Err(error) => {
                    if cancel.is_cancelled() {
                        break;
                    }
                    if error
                        .downcast_ref::<ApiError>()
                        .is_some_and(|e| e.0 < 500 && e.0 != 429)
                    {
                        self.queue.lock().await.error = Some(error.to_string());
                        self.changed.notify_waiters();
                        break;
                    }
                    tokio::select! { _ = cancel.cancelled() => break, _ = tokio::time::sleep(std::time::Duration::from_secs(1)) => {} }
                }
            }
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[tokio::test]
    async fn offline_progress_is_coalesced_and_critical_events_survive_restart() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("events.json");
        let outbox = Outbox::open(path.clone()).unwrap();
        outbox
            .push(json!({"kind":"stage","eventId":"started","stage":"training"}))
            .await
            .unwrap();
        for n in 0..1000 {
            outbox.push(json!({"kind":"progress","eventId":format!("p{n}"),"stage":"training","progress":{"iteration":n}})).await.unwrap();
        }
        outbox
            .push(json!({"kind":"failed","eventId":"terminal"}))
            .await
            .unwrap();
        drop(outbox);
        let restored = Outbox::open(path).unwrap();
        let queue = restored.queue.lock().await;
        assert_eq!(queue.events.len(), 3);
        assert_eq!(queue.events[0]["eventId"], "started");
        assert_eq!(queue.events[1]["progress"]["iteration"], 999);
        assert_eq!(queue.events[2]["eventId"], "terminal");
        assert_eq!(queue.sequence, 1002);
    }
}
