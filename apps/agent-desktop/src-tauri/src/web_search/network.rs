use std::{
    net::{IpAddr, SocketAddr},
    time::Duration,
};

use reqwest::{header::CONTENT_TYPE, redirect::Policy, Client, Method};
use serde_json::Value;
use tokio::net::lookup_host;
use url::Url;

use super::policy::{
    CONNECT_TIMEOUT_SECS, FIRST_RESPONSE_TIMEOUT_SECS, MAX_PAGE_BYTES, MAX_REDIRECTS,
};

#[derive(Clone, Default)]
pub struct SafeNetwork;

impl SafeNetwork {
    pub async fn json(
        &self,
        method: Method,
        url: Url,
        bearer: Option<&str>,
        body: Option<Value>,
        timeout: Duration,
        allow_loopback: bool,
    ) -> Result<Value, String> {
        let bytes = self
            .request_json_bytes(method, url, bearer, body, timeout, allow_loopback)
            .await?;
        serde_json::from_slice(&bytes)
            .map_err(|_| "search_result_invalid: Search service returned invalid JSON".to_string())
    }

    pub async fn text(&self, url: Url, timeout: Duration) -> Result<(Url, String), String> {
        let mut current = url;
        for _ in 0..=MAX_REDIRECTS {
            let (client, resolved) = client_for(&current, false).await?;
            let response = tokio::time::timeout(
                Duration::from_secs(FIRST_RESPONSE_TIMEOUT_SECS),
                client
                    .get(current.clone())
                    .header("Accept", "text/html,text/plain;q=0.9")
                    .timeout(timeout)
                    .send(),
            )
            .await
            .map_err(|_| "search_timeout: Web page did not start responding in time".to_string())?
            .map_err(map_network_error)?;
            if response.status().is_redirection() {
                let location = response
                    .headers()
                    .get(reqwest::header::LOCATION)
                    .and_then(|value| value.to_str().ok())
                    .ok_or_else(|| "search_result_invalid: Redirect has no location".to_string())?;
                current = current
                    .join(location)
                    .map_err(|_| "web_url_blocked: Invalid redirect URL".to_string())?;
                super::policy::validate_public_page_url(current.as_str())?;
                drop(resolved);
                continue;
            }
            if !response.status().is_success() {
                return Err(format!(
                    "search_result_invalid: Web page returned {}",
                    response.status()
                ));
            }
            let content_type = response
                .headers()
                .get(CONTENT_TYPE)
                .and_then(|value| value.to_str().ok())
                .unwrap_or("");
            if !(content_type.starts_with("text/")
                || content_type.starts_with("application/xhtml+xml"))
            {
                return Err("web_url_blocked: Only text and HTML pages can be read".to_string());
            }
            let bytes = limited_bytes(response).await?;
            return String::from_utf8(bytes.to_vec())
                .map(|text| (current, text))
                .map_err(|_| "search_result_invalid: Page is not UTF-8 text".to_string());
        }
        Err("web_url_blocked: Too many redirects".to_string())
    }

    async fn request_json_bytes(
        &self,
        method: Method,
        url: Url,
        bearer: Option<&str>,
        body: Option<Value>,
        timeout: Duration,
        allow_loopback: bool,
    ) -> Result<bytes::Bytes, String> {
        let (client, _resolved) = client_for(&url, allow_loopback).await?;
        let mut request = client.request(method, url).timeout(timeout);
        if let Some(secret) = bearer {
            request = request.bearer_auth(secret);
        }
        if let Some(value) = body {
            request = request.json(&value);
        }
        let response = tokio::time::timeout(
            Duration::from_secs(FIRST_RESPONSE_TIMEOUT_SECS),
            request.send(),
        )
        .await
        .map_err(|_| "search_timeout: Search service did not start responding in time".to_string())?
        .map_err(map_network_error)?;
        if response.status() == reqwest::StatusCode::UNAUTHORIZED
            || response.status() == reqwest::StatusCode::FORBIDDEN
        {
            return Err("search_authentication_failed: Search credential was rejected".to_string());
        }
        if response.status() == reqwest::StatusCode::TOO_MANY_REQUESTS {
            let retry_after = response
                .headers()
                .get(reqwest::header::RETRY_AFTER)
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.parse::<u64>().ok());
            return Err(match retry_after {
                Some(seconds) => format!(
                    "search_rate_limited: Search service rate limit reached; retry_after={seconds}"
                ),
                None => "search_rate_limited: Search service rate limit reached".to_string(),
            });
        }
        if response.status() == reqwest::StatusCode::PAYMENT_REQUIRED
            || response.status().as_u16() == 432
        {
            return Err("search_quota_exhausted: Search service quota is exhausted".to_string());
        }
        if !response.status().is_success() {
            return Err(format!(
                "search_result_invalid: Search service returned {}",
                response.status()
            ));
        }
        let content_type = response
            .headers()
            .get(CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("");
        if !content_type.contains("json") {
            return Err("search_result_invalid: Search service did not return JSON".to_string());
        }
        limited_bytes(response).await
    }
}

async fn client_for(url: &Url, allow_loopback: bool) -> Result<(Client, Vec<SocketAddr>), String> {
    let host = url
        .host_str()
        .ok_or_else(|| "web_url_blocked: Missing host".to_string())?
        .to_string();
    let port = url
        .port_or_known_default()
        .ok_or_else(|| "web_url_blocked: Missing port".to_string())?;
    let resolved = lookup_host((host.as_str(), port))
        .await
        .map_err(|_| "network_unavailable: DNS lookup failed".to_string())?
        .collect::<Vec<_>>();
    if resolved.is_empty() {
        return Err("network_unavailable: DNS returned no address".to_string());
    }
    if resolved
        .iter()
        .any(|address| is_blocked(address.ip(), allow_loopback))
    {
        return Err(
            "web_url_blocked: Private or reserved network address is forbidden".to_string(),
        );
    }
    let mut builder = Client::builder()
        .redirect(Policy::none())
        .connect_timeout(Duration::from_secs(CONNECT_TIMEOUT_SECS));
    for address in &resolved {
        builder = builder.resolve(&host, *address);
    }
    let client = builder
        .build()
        .map_err(|_| "network_unavailable: HTTP client initialization failed".to_string())?;
    Ok((client, resolved))
}

fn is_blocked(ip: IpAddr, allow_loopback: bool) -> bool {
    match ip {
        IpAddr::V4(value) => {
            (!allow_loopback && value.is_loopback())
                || value.is_private()
                || value.is_link_local()
                || value.is_unspecified()
                || value.is_multicast()
                || value.is_broadcast()
                || value.octets()[0] == 0
                || value.octets()[0] >= 224
        }
        IpAddr::V6(value) => {
            (!allow_loopback && value.is_loopback())
                || value.is_unspecified()
                || value.is_multicast()
                || (value.segments()[0] & 0xfe00) == 0xfc00
                || (value.segments()[0] & 0xffc0) == 0xfe80
        }
    }
}

async fn limited_bytes(mut response: reqwest::Response) -> Result<bytes::Bytes, String> {
    if response
        .content_length()
        .is_some_and(|size| size > MAX_PAGE_BYTES as u64)
    {
        return Err("web_content_too_large: Response exceeds the size limit".to_string());
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(map_network_error)? {
        if bytes.len().saturating_add(chunk.len()) > MAX_PAGE_BYTES {
            return Err("web_content_too_large: Response exceeds the size limit".to_string());
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes::Bytes::from(bytes))
}

fn map_network_error(error: reqwest::Error) -> String {
    if error.is_timeout() {
        "search_timeout: Search request timed out".to_string()
    } else if error.is_connect() {
        "network_unavailable: Search service is unreachable".to_string()
    } else {
        "connection_reset: Search connection failed".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blocks_private_reserved_and_loopback_page_addresses() {
        for value in [
            "127.0.0.1",
            "10.0.0.1",
            "192.168.1.2",
            "169.254.1.2",
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fe80::1",
            "fc00::1",
        ] {
            let ip: IpAddr = value.parse().unwrap();
            assert!(is_blocked(ip, false), "{value} should be blocked");
        }
        assert!(!is_blocked("1.1.1.1".parse().unwrap(), false));
    }

    #[test]
    fn loopback_is_only_allowed_for_configured_backend_connections() {
        assert!(!is_blocked("127.0.0.1".parse().unwrap(), true));
        assert!(is_blocked("192.168.1.2".parse().unwrap(), true));
    }
}
