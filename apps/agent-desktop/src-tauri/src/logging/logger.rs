use std::{
    path::{Path, PathBuf},
    sync::Mutex,
};

use chrono::{SecondsFormat, Utc};

use serde_json::{Map, Value};

use super::{
    redaction::{redact_text, redact_value},
    writer::JsonlWriter,
};

/// agent Rust 日志 Schema 版本。
pub const SCHEMA_VERSION: u32 = 1;

/// Rust Host 统一日志等级。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LogLevel {
    Debug,
    Info,
    Warn,
    Error,
}

impl LogLevel {
    fn as_str(self) -> &'static str {
        match self {
            Self::Debug => "DEBUG",
            Self::Info => "INFO",
            Self::Warn => "WARN",
            Self::Error => "ERROR",
        }
    }
}

/// agent Rust Host 日志入口。
///
/// 内部维护两个独立 JSONL Writer：
///
/// host:
///     Rust / Tauri / sidecar 生命周期。
///
/// ipc:
///     Rust ↔ Python IPC 请求链路。
///
/// 业务代码不应该自己打开日志文件。
pub struct AppLogger {
    /// 当前实际使用的系统日志目录。
    ///
    /// None 表示文件日志不可用，
    /// 此时自动退化成 stderr-only。
    log_dir: Option<PathBuf>,

    host_writer: Option<Mutex<JsonlWriter>>,
    ipc_writer: Option<Mutex<JsonlWriter>>,

    debug_enabled: bool,
}

impl AppLogger {
    /// 初始化 Rust 日志系统。
    ///
    /// 某一个文件 Writer 初始化失败时，
    /// 不会阻止 agent 启动。
    ///
    /// 最差会退化成 stderr-only。
    pub fn new(log_dir: impl Into<PathBuf>, debug_enabled: bool) -> Self {
        let log_dir = log_dir.into();

        let host_writer = match JsonlWriter::new(&log_dir, "host") {
            Ok(writer) => Some(Mutex::new(writer)),

            Err(error) => {
                eprintln!(
                    "[agent] Host file logging unavailable: {}",
                    redact_text(&error)
                );

                None
            }
        };

        let ipc_writer = match JsonlWriter::new(&log_dir, "ipc") {
            Ok(writer) => Some(Mutex::new(writer)),

            Err(error) => {
                eprintln!(
                    "[agent] IPC file logging unavailable: {}",
                    redact_text(&error)
                );

                None
            }
        };

        Self {
            log_dir: Some(log_dir),
            host_writer,
            ipc_writer,
            debug_enabled,
        }
    }

    /// 写 Host 事件。
    pub fn host(&self, level: LogLevel, event: &str, message: &str, fields: Value) {
        self.write("host", &self.host_writer, level, event, message, fields);
    }

    /// 写 IPC 事件。
    pub fn ipc(&self, level: LogLevel, event: &str, message: &str, fields: Value) {
        self.write("ipc", &self.ipc_writer, level, event, message, fields);
    }

    fn should_log(&self, level: LogLevel) -> bool {
        match level {
            LogLevel::Debug => self.debug_enabled,

            _ => true,
        }
    }

    fn write(
        &self,
        component: &str,
        writer: &Option<Mutex<JsonlWriter>>,
        level: LogLevel,
        event: &str,
        message: &str,
        fields: Value,
    ) {
        if !self.should_log(level) {
            return;
        }

        let safe_message = redact_text(message);

        let safe_fields = redact_value(&fields);

        // 控制台输出保持人类可读。
        //
        // 使用 stderr，
        // 避免未来 stdout 被其他协议占用。
        eprintln!(
            "{} | {:<5} | {} | {} | {}",
            Self::timestamp(),
            level.as_str(),
            component,
            event,
            safe_message,
        );

        let record = Self::build_record(component, level, event, &safe_message, safe_fields);

        let Some(writer) = writer else {
            return;
        };

        // 日志锁失败或磁盘写入失败，
        // 都不能传播到业务层。
        let Ok(mut writer) = writer.lock() else {
            eprintln!("[agent] Failed to lock {} log writer", component);

            return;
        };

        if let Err(error) = writer.write_value(&record) {
            eprintln!(
                "[agent] Failed to write {} log: {}",
                component,
                redact_text(&error)
            );
        }
    }

    fn build_record(
        component: &str,
        level: LogLevel,
        event: &str,
        message: &str,
        fields: Value,
    ) -> Value {
        let mut object = Map::new();

        object.insert("schema_version".to_string(), Value::from(SCHEMA_VERSION));

        object.insert("timestamp".to_string(), Value::String(Self::timestamp()));

        object.insert(
            "level".to_string(),
            Value::String(level.as_str().to_string()),
        );

        object.insert(
            "component".to_string(),
            Value::String(component.to_string()),
        );

        object.insert("event".to_string(), Value::String(event.to_string()));

        object.insert("message".to_string(), Value::String(message.to_string()));

        // 业务字段允许展开到顶层，
        // 但不能覆盖日志系统保留字段。
        if let Value::Object(fields) = fields {
            for (key, value) in fields {
                if Self::is_reserved_field(&key) {
                    continue;
                }

                object.insert(key, value);
            }
        }

        Value::Object(object)
    }

    fn is_reserved_field(key: &str) -> bool {
        matches!(
            key,
            "schema_version" | "timestamp" | "level" | "component" | "event" | "message"
        )
    }

    fn timestamp() -> String {
        Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true)
    }
    /// 创建不写文件的降级 Logger。
    ///
    /// 当系统日志目录无法解析或创建时，
    /// agent 仍然可以正常运行，
    /// 日志只输出到 stderr。
    pub fn stderr_only(debug_enabled: bool) -> Self {
        Self {
            log_dir: None,
            host_writer: None,
            ipc_writer: None,
            debug_enabled,
        }
    }

    /// 返回 Rust Host 已解析的系统日志目录。
    ///
    /// Python Core 后续也直接复用这个目录，
    /// 不再自己或 CoreProcessManager 重复计算平台路径。
    pub fn log_dir(&self) -> Option<&Path> {
        self.log_dir.as_deref()
    }
}

#[cfg(test)]
mod tests {
    use serde_json::{json, Value};

    use super::{AppLogger, LogLevel};

    #[test]
    fn writes_host_jsonl_record() {
        let dir = std::env::temp_dir().join(format!("agent-logger-test-{}", uuid::Uuid::new_v4()));

        let logger = AppLogger::new(&dir, true);

        logger.host(
            LogLevel::Info,
            "host_started",
            "Host started",
            json!({
                "pid": 12345
            }),
        );

        let host_file = std::fs::read_dir(&dir)
            .unwrap()
            .filter_map(|entry| entry.ok())
            .map(|entry| entry.path())
            .find(|path| {
                path.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with("host-")
            })
            .expect("host log should exist");

        let line = std::fs::read_to_string(host_file).unwrap();

        let value: Value = serde_json::from_str(line.trim()).unwrap();

        assert_eq!(value["schema_version"], 1);

        assert_eq!(value["component"], "host");

        assert_eq!(value["event"], "host_started");

        assert_eq!(value["pid"], 12345);

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn redacts_sensitive_fields_before_writing() {
        let dir = std::env::temp_dir().join(format!("agent-logger-test-{}", uuid::Uuid::new_v4()));

        let logger = AppLogger::new(&dir, true);

        logger.ipc(
            LogLevel::Info,
            "ipc_test",
            "IPC test",
            json!({
                "api_key":
                    "sk-super-secret123456",
                "request_id":
                    "req-001"
            }),
        );

        let text = std::fs::read_dir(&dir)
            .unwrap()
            .filter_map(|entry| entry.ok())
            .map(|entry| entry.path())
            .find(|path| {
                path.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with("ipc-")
            })
            .map(|path| std::fs::read_to_string(path).unwrap())
            .expect("ipc log should exist");

        assert!(!text.contains("sk-super-secret123456"));

        assert!(text.contains("[REDACTED]"));

        let _ = std::fs::remove_dir_all(&dir);
    }
}
