use super::provider::{NativeSystemLocaleProvider, SystemLocaleProvider};

/// 返回操作系统当前 Locale。
///
/// 返回值保持平台 Locale 语义，例如：
///
/// zh-CN
/// en-US
/// ja-JP
///
/// 这里不决定 agent 最终使用哪种语言。
#[tauri::command]
pub fn get_system_locale() -> Result<String, String> {
    let provider = NativeSystemLocaleProvider;

    provider.get_locale().map_err(|error| error.to_string())
}
