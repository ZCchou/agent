use std::{
    fs,
    path::{Path, PathBuf},
};

use chrono::{Local, NaiveDate};
use regex::Regex;

pub const DEFAULT_RETENTION_DAYS: i64 = 14;

pub const DEFAULT_MAX_TOTAL_BYTES: u64 = 200 * 1024 * 1024;

#[derive(Debug)]
struct LogFileInfo {
    path: PathBuf,
    log_date: NaiveDate,
    index: u32,
    size: u64,
}

/// 一次日志清理的结果。
#[derive(Debug, Default)]
pub struct RetentionResult {
    pub deleted_files: usize,
    pub failed_files: usize,
    pub freed_bytes: u64,
    pub remaining_bytes: u64,
}

/// 执行 agent 默认日志保留策略。
pub fn cleanup_logs(log_dir: &Path) -> Result<RetentionResult, String> {
    cleanup_logs_with_policy(
        log_dir,
        Local::now().date_naive(),
        DEFAULT_RETENTION_DAYS,
        DEFAULT_MAX_TOTAL_BYTES,
    )
}

/// 实际执行保留策略。
///
/// 独立出 today / retention_days / max_total_bytes，
/// 是为了让日期边界和容量策略可以稳定测试。
fn cleanup_logs_with_policy(
    log_dir: &Path,
    today: NaiveDate,
    retention_days: i64,
    max_total_bytes: u64,
) -> Result<RetentionResult, String> {
    if retention_days <= 0 {
        return Err("retention_days must be greater than 0".to_string());
    }

    if !log_dir.exists() {
        return Ok(RetentionResult::default());
    }

    let oldest_allowed = today
        .checked_sub_signed(chrono::Duration::days(retention_days - 1))
        .ok_or_else(|| "Failed to calculate log retention boundary".to_string())?;

    let mut result = RetentionResult::default();

    // =====================================================
    // 第一阶段：删除超过保留天数的日志
    // =====================================================

    let files = collect_log_files(log_dir)?;

    for info in files {
        if info.log_date < oldest_allowed {
            match fs::remove_file(&info.path) {
                Ok(()) => {
                    result.deleted_files += 1;
                    result.freed_bytes += info.size;
                }

                Err(_) => {
                    result.failed_files += 1;
                }
            }
        }
    }

    // =====================================================
    // 第二阶段：限制日志目录总大小
    // =====================================================

    let mut remaining = collect_log_files(log_dir)?;

    let mut total_size: u64 = remaining.iter().map(|info| info.size).sum();

    // 按：
    //
    // 日期
    // ↓
    // 同一天的轮转 index
    //
    // 从旧到新排序。
    remaining.sort_by(|left, right| {
        (left.log_date, left.index, &left.path).cmp(&(right.log_date, right.index, &right.path))
    });

    for info in remaining {
        if total_size <= max_total_bytes {
            break;
        }

        match fs::remove_file(&info.path) {
            Ok(()) => {
                total_size = total_size.saturating_sub(info.size);

                result.deleted_files += 1;

                result.freed_bytes += info.size;
            }

            Err(_) => {
                // Windows 上文件可能正被占用。
                //
                // 日志清理失败不能阻止应用启动。
                result.failed_files += 1;
            }
        }
    }

    // 最后重新计算真实剩余大小，
    // 避免删除失败造成统计误差。
    result.remaining_bytes = collect_log_files(log_dir)?
        .iter()
        .map(|info| info.size)
        .sum();

    Ok(result)
}

/// 收集 agent 自己管理的日志文件。
///
/// 不符合命名规则的文件完全忽略。
fn collect_log_files(log_dir: &Path) -> Result<Vec<LogFileInfo>, String> {
    let pattern = Regex::new(r"^(core|host|ipc)-(\d{4}-\d{2}-\d{2})(?:\.(\d{3}))?\.jsonl$")
        .expect("log filename regex must be valid");

    let entries =
        fs::read_dir(log_dir).map_err(|error| format!("Failed to read log directory: {error}"))?;

    let mut files = Vec::new();

    for entry in entries {
        let Ok(entry) = entry else {
            continue;
        };

        let path = entry.path();

        if !path.is_file() {
            continue;
        }

        let Some(filename) = path.file_name().and_then(|name| name.to_str()) else {
            continue;
        };

        let Some(captures) = pattern.captures(filename) else {
            continue;
        };

        let Some(date_text) = captures.get(2) else {
            continue;
        };

        let Ok(log_date) = NaiveDate::parse_from_str(date_text.as_str(), "%Y-%m-%d") else {
            continue;
        };

        let index = captures
            .get(3)
            .and_then(|value| value.as_str().parse::<u32>().ok())
            .unwrap_or(0);

        let Ok(metadata) = path.metadata() else {
            continue;
        };

        files.push(LogFileInfo {
            path,
            log_date,
            index,
            size: metadata.len(),
        });
    }

    Ok(files)
}

#[cfg(test)]
mod tests {
    use std::fs;

    use chrono::NaiveDate;

    use super::cleanup_logs_with_policy;

    fn make_temp_dir() -> std::path::PathBuf {
        let dir =
            std::env::temp_dir().join(format!("agent-retention-test-{}", uuid::Uuid::new_v4()));

        fs::create_dir_all(&dir).unwrap();

        dir
    }

    fn create_file(dir: &std::path::Path, name: &str, size: usize) -> std::path::PathBuf {
        let path = dir.join(name);

        fs::write(&path, vec![b'x'; size]).unwrap();

        path
    }

    #[test]
    fn removes_logs_older_than_retention_period() {
        let dir = make_temp_dir();

        let old = create_file(&dir, "core-2026-08-30.jsonl", 100);

        let boundary = create_file(&dir, "host-2026-08-31.jsonl", 100);

        let recent = create_file(&dir, "ipc-2026-09-13.jsonl", 100);

        let today = NaiveDate::from_ymd_opt(2026, 9, 13).unwrap();

        let result = cleanup_logs_with_policy(&dir, today, 14, 10_000).unwrap();

        assert!(!old.exists());

        assert!(boundary.exists());

        assert!(recent.exists());

        assert_eq!(result.deleted_files, 1);

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn deletes_oldest_logs_when_size_limit_is_exceeded() {
        let dir = make_temp_dir();

        let oldest = create_file(&dir, "host-2026-09-10.jsonl", 100);

        let middle = create_file(&dir, "ipc-2026-09-11.jsonl", 100);

        let newest = create_file(&dir, "core-2026-09-12.jsonl", 100);

        let today = NaiveDate::from_ymd_opt(2026, 9, 13).unwrap();

        let result = cleanup_logs_with_policy(&dir, today, 14, 200).unwrap();

        assert!(!oldest.exists());

        assert!(middle.exists());

        assert!(newest.exists());

        assert!(result.remaining_bytes <= 200);

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn does_not_touch_unrelated_files() {
        let dir = make_temp_dir();

        let unrelated = create_file(&dir, "user-data.jsonl", 100);

        create_file(&dir, "core-2026-01-01.jsonl", 100);

        let today = NaiveDate::from_ymd_opt(2026, 9, 13).unwrap();

        cleanup_logs_with_policy(&dir, today, 14, 0).unwrap();

        assert!(unrelated.exists());

        let _ = fs::remove_dir_all(&dir);
    }
}
