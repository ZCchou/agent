use regex::Regex;
use serde_json::{Map, Value};

/// 日志中统一使用的脱敏占位符。
pub const REDACTED: &str = "[REDACTED]";

/// 判断字段名是否属于敏感字段。
///
/// 支持：
///
/// api_key
/// openai_api_key
/// password
/// github_access_token
/// client-secret
fn is_sensitive_key(key: &str) -> bool {
    let normalized = key.trim().to_ascii_lowercase().replace(['-', ' '], "_");

    const SENSITIVE_KEYS: &[&str] = &[
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "client_secret",
        "password",
        "authorization",
        "cookie",
        "set_cookie",
    ];

    SENSITIVE_KEYS
        .iter()
        .any(|sensitive| normalized == *sensitive || normalized.ends_with(&format!("_{sensitive}")))
}

/// 清理普通字符串中的常见秘密。
///
/// 这是日志系统的最后一道保护，
/// 不能替代 Stronghold 等真正的秘密存储。
pub fn redact_text(text: &str) -> String {
    let bearer =
        Regex::new(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+").expect("Bearer regex must be valid");

    let key_value = Regex::new(
        r"(?i)\b(api[_-]?key|token|access[_-]?token|refresh[_-]?token|password|secret|client[_-]?secret)\s*[:=]\s*[^\s,;]+"
    )
    .expect(
        "secret key/value regex must be valid"
    );

    let sk_key = Regex::new(r"\bsk-[A-Za-z0-9_-]{8,}\b").expect("API key regex must be valid");

    let result = bearer.replace_all(text, "Bearer [REDACTED]");

    let result = key_value.replace_all(&result, "$1=[REDACTED]");

    sk_key.replace_all(&result, REDACTED).into_owned()
}

/// 递归清理结构化 JSON 数据。
///
/// Object 中的敏感字段会直接替换；
/// String 中可能存在的秘密也会继续扫描。
pub fn redact_value(value: &Value) -> Value {
    match value {
        Value::Object(object) => {
            let mut cleaned = Map::new();

            for (key, value) in object {
                if is_sensitive_key(key) {
                    cleaned.insert(key.clone(), Value::String(REDACTED.to_string()));
                } else {
                    cleaned.insert(key.clone(), redact_value(value));
                }
            }

            Value::Object(cleaned)
        }

        Value::Array(items) => Value::Array(items.iter().map(redact_value).collect()),

        Value::String(text) => Value::String(redact_text(text)),

        _ => value.clone(),
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{redact_text, redact_value, REDACTED};

    #[test]
    fn redacts_sensitive_fields() {
        let value = json!({
            "api_key": "sk-super-secret123456",
            "password": "123456",
            "provider": "openai"
        });

        let result = redact_value(&value);

        assert_eq!(result["api_key"], REDACTED);

        assert_eq!(result["password"], REDACTED);

        assert_eq!(result["provider"], "openai");
    }

    #[test]
    fn redacts_nested_fields() {
        let value = json!({
            "provider": {
                "github_access_token": "secret-token",
                "config": {
                    "client_secret": "very-secret"
                }
            }
        });

        let result = redact_value(&value);

        assert_eq!(result["provider"]["github_access_token"], REDACTED);

        assert_eq!(result["provider"]["config"]["client_secret"], REDACTED);
    }

    #[test]
    fn redacts_bearer_token_in_text() {
        let result = redact_text("Authorization: Bearer abc.def.ghi");

        assert!(!result.contains("abc.def.ghi"));

        assert!(result.contains(REDACTED));
    }
}
