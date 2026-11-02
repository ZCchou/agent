use std::{error::Error, fmt};

/// 系统 Locale 读取失败。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LocaleError {
    message: String,
}

impl LocaleError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for LocaleError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}", self.message,)
    }
}

impl Error for LocaleError {}

/// 系统 Locale 数据源边界。
///
/// 当前正式实现使用 sys-locale。
/// 如果未来某个平台需要单独实现，
/// 上层 command 不需要改变。
pub trait SystemLocaleProvider {
    fn get_locale(&self) -> Result<String, LocaleError>;
}

/// agent 当前正式的 Native Locale Provider。
pub struct NativeSystemLocaleProvider;

impl SystemLocaleProvider for NativeSystemLocaleProvider {
    fn get_locale(&self) -> Result<String, LocaleError> {
        let raw_locale = sys_locale::get_locale()
            .ok_or_else(|| LocaleError::new("operating system did not provide a locale"))?;

        normalize_locale(&raw_locale)
    }
}

/// 将不同平台可能返回的 Locale 格式统一成
/// agent 对外使用的连字符形式。
///
/// 例如：
///
/// zh_CN -> zh-CN
/// en_US -> en-US
///
/// 不在这里把 zh-CN 映射成应用语言，
/// 那属于前端 LanguageResolver 的职责。
pub fn normalize_locale(locale: &str) -> Result<String, LocaleError> {
    let locale = locale.trim();

    if locale.is_empty() {
        return Err(LocaleError::new(
            "operating system returned an empty locale",
        ));
    }

    Ok(locale.replace('_', "-"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_standard_bcp47_locale_unchanged() {
        assert_eq!(normalize_locale("zh-CN",).unwrap(), "zh-CN",);
    }

    #[test]
    fn converts_underscore_locale_to_hyphen_form() {
        assert_eq!(normalize_locale("en_US",).unwrap(), "en-US",);
    }

    #[test]
    fn trims_surrounding_whitespace() {
        assert_eq!(normalize_locale("  ja-JP  ",).unwrap(), "ja-JP",);
    }

    #[test]
    fn rejects_empty_locale() {
        assert!(normalize_locale("   ",).is_err(),);
    }
}
