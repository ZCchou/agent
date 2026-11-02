use url::Url;

pub const CONNECT_TIMEOUT_SECS: u64 = 10;
pub const FIRST_RESPONSE_TIMEOUT_SECS: u64 = 30;
pub const REQUEST_TIMEOUT_SECS: u64 = 90;
pub const PAGE_TIMEOUT_SECS: u64 = 30;
pub const MAX_PAGE_BYTES: usize = 5 * 1024 * 1024;
pub const MAX_REDIRECTS: usize = 3;

pub fn validate_backend_url(value: &str) -> Result<Url, String> {
    let url =
        Url::parse(value).map_err(|_| "search_backend_invalid: Invalid backend URL".to_string())?;
    validate_credentials(&url)?;
    let host = url.host_str().unwrap_or_default();
    let loopback = matches!(host, "localhost" | "127.0.0.1" | "::1");
    if url.scheme() != "https" && !(loopback && url.scheme() == "http") {
        return Err("search_backend_invalid: Remote search services must use HTTPS".to_string());
    }
    Ok(url)
}

pub fn validate_public_page_url(value: &str) -> Result<Url, String> {
    let url = Url::parse(value).map_err(|_| "web_url_blocked: Invalid URL".to_string())?;
    validate_credentials(&url)?;
    if url.scheme() != "https" {
        return Err("web_url_blocked: Web pages must use HTTPS".to_string());
    }
    Ok(url)
}

fn validate_credentials(url: &Url) -> Result<(), String> {
    if !url.username().is_empty() || url.password().is_some() {
        return Err("web_url_blocked: URL credentials are forbidden".to_string());
    }
    if url.host_str().is_none() {
        return Err("web_url_blocked: URL host is missing".to_string());
    }
    Ok(())
}
