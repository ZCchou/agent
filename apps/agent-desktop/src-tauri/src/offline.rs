use serde_json::Value;
pub const OFFLINE_ERROR: &str =
    "offline_operation_disabled: 离线模式禁用公网服务、检索、远程图片和在线更新。";
pub fn deny_network() -> Result<(), String> {
    Err(OFFLINE_ERROR.into())
}
pub fn local_endpoint(value: &str) -> Result<String, String> {
    // Check the literal authority before URL parsing, which normalizes 127.1 and integer hosts.
    let authority = value
        .split_once("://")
        .map(|(_, rest)| rest.split(['/', '?', '#']).next().unwrap_or(""))
        .ok_or(OFFLINE_ERROR)?;
    let raw_host = if authority.starts_with('[') {
        authority
            .split_once(']')
            .map(|(host, _)| format!("{host}]"))
    } else {
        Some(
            authority
                .split(':')
                .next()
                .unwrap_or("")
                .to_ascii_lowercase(),
        )
    };
    if !raw_host
        .as_deref()
        .is_some_and(|host| matches!(host, "localhost" | "127.0.0.1" | "[::1]"))
    {
        return Err(OFFLINE_ERROR.into());
    }
    let mut parsed = url::Url::parse(value).map_err(|_| OFFLINE_ERROR)?;
    if !matches!(parsed.scheme(), "http" | "https")
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        return Err(OFFLINE_ERROR.into());
    }
    if parsed.host_str() == Some("localhost") {
        parsed
            .set_host(Some("127.0.0.1"))
            .map_err(|_| OFFLINE_ERROR)?;
    }
    Ok(parsed.to_string().trim_end_matches('/').to_string())
}
pub fn validate_profile(
    profile: &crate::settings::model::ProviderProfileSettings,
) -> Result<(), String> {
    if profile.id.starts_with("managed:") {
        return Ok(());
    }
    if profile.kind == "fake" {
        return Ok(());
    }
    local_endpoint(profile.effective_base_url())?;
    if profile.native_search.adapter_id != "none" {
        return Err(OFFLINE_ERROR.into());
    }
    for route in &profile.invocation_routes {
        if let Some(endpoint) = &route.endpoint_base_url {
            local_endpoint(endpoint)?;
        }
    }
    Ok(())
}
pub fn validate_dispatch(message_type: &str, params: &Value) -> Result<(), String> {
    if message_type == "provider.configure" {
        if params["kind"] == "fake" {
            return Ok(());
        }
        local_endpoint(params["base_url"].as_str().ok_or(OFFLINE_ERROR)?)?;
        if params["native_search"]["adapter_id"]
            .as_str()
            .is_some_and(|id| id != "none")
        {
            return Err(OFFLINE_ERROR.into());
        }
        if let Some(routes) = params["invocation_routes"].as_array() {
            for route in routes {
                if let Some(endpoint) = route["endpoint_base_url"].as_str() {
                    local_endpoint(endpoint)?;
                }
            }
        }
    }
    if message_type.starts_with("web.search")
        || message_type.starts_with("web_search")
        || message_type == "model.native_web_search.probe"
    {
        return deny_network();
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strict_numeric_loopback_and_localhost_only() {
        for address in [
            "http://127.0.0.1:8080/v1",
            "http://localhost:8080",
            "https://[::1]:8443",
        ] {
            assert!(local_endpoint(address).is_ok());
        }
        assert_eq!(
            local_endpoint("http://localhost:8080/v1").unwrap(),
            "http://127.0.0.1:8080/v1"
        );
        for address in [
            "https://api.openai.com",
            "http://127.1",
            "http://2130706433",
            "http://localhost.evil",
            "http://127.0.0.1@evil",
            "http://user@localhost",
            "http://localhost?token=abc",
            "http://localhost#x",
            "http://localhost:99999",
        ] {
            assert!(local_endpoint(address).is_err(), "{address}");
        }
    }
    #[test]
    fn public_dispatch_rejects_saved_remote_routes_and_search() {
        assert!(validate_dispatch(
            "provider.configure",
            &serde_json::json!({"kind":"openai_compatible", "base_url":"https://api.openai.com/v1"})
        )
        .is_err());
        assert!(validate_dispatch("provider.configure", &serde_json::json!({"kind":"ollama", "base_url":"http://localhost:11434", "invocation_routes":[{"endpoint_base_url":"https://remote.test"}]})).is_err());
        assert!(validate_dispatch("web.search", &serde_json::json!({})).is_err());
    }
}
