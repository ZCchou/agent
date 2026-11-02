//! agent Rust 日志基础设施。
//!
//! 业务模块只应通过这里暴露的 API 使用日志。
//! 不应该直接操作 JSONL 文件。

pub mod events;
pub mod logger;
pub mod redaction;
pub mod retention;
pub mod writer;

pub use logger::{AppLogger, LogLevel};
