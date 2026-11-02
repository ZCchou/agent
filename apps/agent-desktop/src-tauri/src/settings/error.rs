use std::{error::Error, fmt};

/// Settings 子系统统一错误类型。
///
/// 内部保留错误类别，Tauri command 最终再转换成
/// 用户界面可以处理的字符串或错误码。
#[derive(Debug)]
pub enum SettingsError {
    Storage(String),
    Serialization(String),
    InvalidData(String),
    UnsupportedSchemaVersion(u32),
}

impl fmt::Display for SettingsError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Storage(message) => {
                write!(formatter, "settings storage error: {message}")
            }

            Self::Serialization(message) => {
                write!(formatter, "settings serialization error: {message}")
            }

            Self::InvalidData(message) => {
                write!(formatter, "invalid settings data: {message}")
            }

            Self::UnsupportedSchemaVersion(version) => {
                write!(formatter, "unsupported settings schema version: {version}")
            }
        }
    }
}

impl Error for SettingsError {}
