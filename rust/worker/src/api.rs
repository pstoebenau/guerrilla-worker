use anyhow::{bail, Result};
use reqwest::{Client as Http, Url};
use serde::de::DeserializeOwned;
use serde_json::Value;
use std::time::Duration;
use tokio_util::sync::CancellationToken;

#[derive(Debug)]
pub struct ApiError(pub u16);
impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Worker API rejected request ({})", self.0)
    }
}
impl std::error::Error for ApiError {}
pub fn origin(value: &str) -> Result<String> {
    let url = Url::parse(value).map_err(|_| anyhow::anyhow!("Invalid control plane URL"))?;
    if url.scheme() != "https"
        && !(url.scheme() == "http"
            && matches!(
                url.host_str(),
                Some("localhost" | "127.0.0.1" | "[::1]" | "::1")
            ))
    {
        bail!("Control plane requires HTTPS");
    }
    if !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        bail!("Invalid control plane origin");
    }
    Ok(url.origin().ascii_serialization())
}
pub fn transfer_url(value: &str) -> Result<Url> {
    let url = Url::parse(value).map_err(|_| anyhow::anyhow!("Invalid transfer URL"))?;
    if !matches!(url.scheme(), "https" | "http")
        || !url.username().is_empty()
        || url.password().is_some()
    {
        bail!("Invalid transfer URL");
    }
    Ok(url)
}
#[derive(Clone)]
pub struct Client {
    pub origin: String,
    credential: String,
    pub http: Http,
    pub session: Option<std::sync::Arc<crate::connection::Session>>,
}
impl Client {
    pub fn new(url: &str, credential: &str) -> Result<Self> {
        Ok(Self {
            origin: origin(url)?,
            credential: credential.into(),
            session: None,
            http: Http::builder()
                .redirect(reqwest::redirect::Policy::none())
                .build()?,
        })
    }
    pub fn credential(&self) -> &str {
        &self.credential
    }
    pub async fn wait_connected(&self, cancel: &CancellationToken) -> Result<()> {
        if cancel.is_cancelled() {
            bail!("Worker operation interrupted");
        }
        if let Some(session) = &self.session {
            session.wait(cancel).await?;
        }
        if cancel.is_cancelled() {
            bail!("Worker operation interrupted");
        }
        Ok(())
    }
    pub async fn post<T: DeserializeOwned>(
        &self,
        route: &str,
        body: &Value,
        cancel: &CancellationToken,
    ) -> Result<T> {
        let mut attempt = 0u32;
        loop {
            self.wait_connected(cancel).await?;
            let request = self
                .http
                .post(format!("{}/api/worker/{}", self.origin, route))
                .timeout(Duration::from_secs(
                    if matches!(route, "checkpoint" | "complete") {
                        600
                    } else {
                        30
                    },
                ))
                .json(body);
            let request = if self.credential.is_empty() {
                request
            } else {
                request.bearer_auth(&self.credential)
            };
            let result = tokio::select! {_ = cancel.cancelled()=>bail!("Worker operation interrupted"),result=async {let response=request.send().await.map_err(|_|anyhow::anyhow!("Worker {route} transport failed"))?; if !response.status().is_success(){return Err(anyhow::Error::new(ApiError(response.status().as_u16())));} response.json().await.map_err(|_|anyhow::anyhow!("Invalid worker {route} response"))}=>result};
            match result {
                Ok(value) => return Ok(value),
                Err(e) => {
                    if let Some(session) = &self.session {
                        session.disconnect();
                    }
                    let fatal = e
                        .downcast_ref::<ApiError>()
                        .is_some_and(|s| s.0 < 500 && s.0 != 429);
                    if (attempt == 2 && self.session.is_none()) || fatal {
                        return Err(e);
                    }
                    tokio::select! {_=cancel.cancelled()=>bail!("Worker operation interrupted"),_=tokio::time::sleep(Duration::from_millis(500 * (1<<attempt.min(4))))=>{}}
                    attempt = attempt.saturating_add(1);
                }
            }
        }
    }
}
pub fn body(fence: &crate::protocol::Fence, extra: Value) -> Value {
    let mut value = serde_json::to_value(fence).unwrap();
    if let (Some(base), Some(extra)) = (value.as_object_mut(), extra.as_object()) {
        base.extend(extra.clone());
    }
    value
}
