use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

use chrono::Local;
use serde_json::Value;

pub const DEFAULT_MAX_BYTES: u64 = 20 * 1024 * 1024;

/// agent Rust JSONL 文件写入器。
///
/// 每个实例只负责一个 component，例如：
///
/// - host
/// - ipc
///
/// 文件格式：
///
/// host-2026-09-13.jsonl
///
/// 超过大小限制后：
///
/// host-2026-09-13.001.jsonl
/// host-2026-09-13.002.jsonl
pub struct JsonlWriter {
    log_dir: PathBuf,
    component: String,
    max_bytes: u64,

    current_date: String,
    file_index: u32,

    file: Option<File>,
}

impl JsonlWriter {
    /// 创建一个新的 JSONL Writer。
    ///
    /// 这里不会立即创建日志文件；
    /// 第一次真正写日志时才打开文件。
    pub fn new(log_dir: impl Into<PathBuf>, component: impl Into<String>) -> Result<Self, String> {
        Self::with_max_bytes(log_dir, component, DEFAULT_MAX_BYTES)
    }

    /// 允许测试时指定更小的轮转大小。
    pub fn with_max_bytes(
        log_dir: impl Into<PathBuf>,
        component: impl Into<String>,
        max_bytes: u64,
    ) -> Result<Self, String> {
        let log_dir = log_dir.into();

        fs::create_dir_all(&log_dir)
            .map_err(|error| format!("Failed to create log directory: {error}"))?;

        let current_date = Self::today_string();

        let component = component.into();

        let file_index = Self::find_available_index(&log_dir, &component, &current_date, max_bytes);

        Ok(Self {
            log_dir,
            component,
            max_bytes,
            current_date,
            file_index,
            file: None,
        })
    }

    fn today_string() -> String {
        Local::now().format("%Y-%m-%d").to_string()
    }

    fn build_path_for(log_dir: &Path, component: &str, date: &str, index: u32) -> PathBuf {
        let filename = if index == 0 {
            format!("{component}-{date}.jsonl")
        } else {
            format!("{component}-{date}.{index:03}.jsonl")
        };

        log_dir.join(filename)
    }

    fn current_path(&self) -> PathBuf {
        Self::build_path_for(
            &self.log_dir,
            &self.component,
            &self.current_date,
            self.file_index,
        )
    }

    fn find_available_index(log_dir: &Path, component: &str, date: &str, max_bytes: u64) -> u32 {
        let mut index = 0;

        loop {
            let path = Self::build_path_for(log_dir, component, date, index);

            if !path.exists() {
                return index;
            }

            match path.metadata() {
                Ok(metadata) if metadata.len() < max_bytes => {
                    return index;
                }

                _ => {
                    index += 1;
                }
            }
        }
    }

    fn open_current_file(&mut self) -> Result<(), String> {
        let path = self.current_path();

        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .map_err(|error| format!("Failed to open log file {}: {error}", path.display()))?;

        self.file = Some(file);

        Ok(())
    }

    fn current_size(&self) -> u64 {
        let path = self.current_path();

        path.metadata().map(|metadata| metadata.len()).unwrap_or(0)
    }

    /// 检查日期是否发生变化。
    ///
    /// Rust Host 长期运行跨过午夜时，
    /// 自动切换到新日期文件。
    fn rotate_for_date_if_needed(&mut self) {
        let today = Self::today_string();

        if today == self.current_date {
            return;
        }

        self.file = None;
        self.current_date = today;

        self.file_index = Self::find_available_index(
            &self.log_dir,
            &self.component,
            &self.current_date,
            self.max_bytes,
        );
    }

    /// 检查写入当前记录后是否超过大小限制。
    fn rotate_for_size_if_needed(&mut self, incoming_bytes: u64) {
        let current_size = self.current_size();

        // 单条日志自身超过限制时仍允许写入，
        // 避免无限轮转。
        if current_size == 0 {
            return;
        }

        if current_size + incoming_bytes <= self.max_bytes {
            return;
        }

        self.file = None;
        self.file_index += 1;
    }

    /// 写入一条已经结构化好的 JSON。
    ///
    /// 每个 Value 最终对应 JSONL 中的一整行。
    pub fn write_value(&mut self, value: &Value) -> Result<(), String> {
        self.rotate_for_date_if_needed();

        let mut line = serde_json::to_string(value)
            .map_err(|error| format!("Failed to serialize log record: {error}"))?;

        line.push('\n');

        let incoming_bytes = line.len() as u64;

        self.rotate_for_size_if_needed(incoming_bytes);

        if self.file.is_none() {
            self.open_current_file()?;
        }

        let file = self
            .file
            .as_mut()
            .ok_or_else(|| "Log file handle is missing".to_string())?;

        file.write_all(line.as_bytes())
            .map_err(|error| format!("Failed to write log record: {error}"))?;

        // 当前阶段先保证日志及时落盘。
        //
        // 后续如果性能测试证明频繁 flush 成为瓶颈，
        // 再改成缓冲/异步批量写。
        file.flush()
            .map_err(|error| format!("Failed to flush log file: {error}"))?;

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::JsonlWriter;

    #[test]
    fn writes_one_json_object_per_line() {
        let temp_dir =
            std::env::temp_dir().join(format!("agent-log-test-{}", uuid::Uuid::new_v4()));

        let mut writer = JsonlWriter::new(&temp_dir, "host").expect("writer should initialize");

        writer
            .write_value(&json!({
                "event": "host_started"
            }))
            .expect("first record should write");

        writer
            .write_value(&json!({
                "event": "host_stopped"
            }))
            .expect("second record should write");

        let files: Vec<_> = std::fs::read_dir(&temp_dir)
            .unwrap()
            .filter_map(|entry| entry.ok())
            .collect();

        assert_eq!(files.len(), 1);

        let text = std::fs::read_to_string(files[0].path()).unwrap();

        assert_eq!(text.lines().count(), 2);

        let _ = std::fs::remove_dir_all(&temp_dir);
    }

    #[test]
    fn rotates_when_size_limit_is_reached() {
        let temp_dir =
            std::env::temp_dir().join(format!("agent-log-test-{}", uuid::Uuid::new_v4()));

        let mut writer =
            JsonlWriter::with_max_bytes(&temp_dir, "host", 250).expect("writer should initialize");

        for index in 0..6 {
            writer
                .write_value(&json!({
                    "event": "rotation_test",
                    "index": index,
                    "message": "x".repeat(180)
                }))
                .expect("record should write");
        }

        let files: Vec<_> = std::fs::read_dir(&temp_dir)
            .unwrap()
            .filter_map(|entry| entry.ok())
            .collect();

        assert!(files.len() >= 2);

        let _ = std::fs::remove_dir_all(&temp_dir);
    }
}
