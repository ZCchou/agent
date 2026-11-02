//! agent Rust Host 日志事件名称。
//!
//! 业务代码应优先使用这些常量，
//! 不要在各处随意手写事件字符串。

// =========================================================
// Host 生命周期
// =========================================================

pub const HOST_STARTED: &str = "host_started";
pub const HOST_STOPPED: &str = "host_stopped";

pub const CORE_START_REQUESTED: &str = "core_start_requested";
pub const CORE_STARTED: &str = "core_started";
pub const CORE_START_FAILED: &str = "core_start_failed";

pub const CORE_STOP_REQUESTED: &str = "core_stop_requested";
pub const CORE_STOPPED: &str = "core_stopped";
pub const CORE_STOP_FAILED: &str = "core_stop_failed";
pub const CORE_TERMINATED: &str = "core_terminated";

// =========================================================
// IPC
// =========================================================

pub const IPC_REQUEST_STARTED: &str = "ipc_request_started";
pub const IPC_REQUEST_COMPLETED: &str = "ipc_request_completed";
pub const IPC_REQUEST_FAILED: &str = "ipc_request_failed";
pub const IPC_RESPONSE_UNMATCHED: &str = "ipc_response_unmatched";
pub const IPC_INVALID_RESPONSE: &str = "ipc_invalid_response";

// =========================================================
// Logging
// =========================================================

pub const LOGGING_STARTED: &str = "logging_started";
pub const LOGGING_DEGRADED: &str = "logging_degraded";
pub const LOG_RETENTION_COMPLETED: &str = "log_retention_completed";
