//! Enrollment errors that the desktop can display without exposing process output or tokens.
#[derive(Debug)]
pub struct EnrollmentError(pub &'static str);

impl std::fmt::Display for EnrollmentError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Enrollment failed ({})", self.0)
    }
}

impl std::error::Error for EnrollmentError {}

pub fn error(code: &'static str) -> anyhow::Error {
    EnrollmentError(code).into()
}

pub fn server_error(cause: anyhow::Error) -> anyhow::Error {
    error(match cause.downcast_ref::<crate::api::ApiError>() {
        Some(crate::api::ApiError(401 | 403)) => "invalid_token",
        Some(crate::api::ApiError(400 | 422)) => "invalid_registration",
        Some(crate::api::ApiError(404)) => "wrong_server",
        Some(crate::api::ApiError(409)) => "runtime_mismatch",
        Some(crate::api::ApiError(429)) => "rate_limited",
        _ => "server_unavailable",
    })
}
