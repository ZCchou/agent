use std::time::Duration;

use regex::Regex;
use reqwest::Method;
use sha2::{Digest, Sha256};

use crate::web_search::{
    domain::{ExtractedPage, SearchRequest, SearchResultSet, WebSource},
    network::SafeNetwork,
    policy::{
        validate_backend_url, validate_public_page_url, PAGE_TIMEOUT_SECS, REQUEST_TIMEOUT_SECS,
    },
};

pub async fn search(
    network: &SafeNetwork,
    base_url: &str,
    request: &SearchRequest,
) -> Result<SearchResultSet, String> {
    let mut url = validate_backend_url(base_url)?;
    url.set_path(&format!("{}/search", url.path().trim_end_matches('/')));
    url.query_pairs_mut()
        .append_pair("q", &request.query)
        .append_pair("format", "json")
        .append_pair("language", &request.language)
        .append_pair("safesearch", if request.safe_search { "1" } else { "0" });
    if let Some(time_range) = request.time_range.as_deref() {
        url.query_pairs_mut().append_pair("time_range", time_range);
    }
    let allow_loopback = url
        .host_str()
        .is_some_and(|host| matches!(host, "localhost" | "127.0.0.1" | "::1"));
    let value = network
        .json(
            Method::GET,
            url,
            None,
            None,
            Duration::from_secs(REQUEST_TIMEOUT_SECS),
            allow_loopback,
        )
        .await?;
    let results = value
        .get("results")
        .and_then(|item| item.as_array())
        .ok_or_else(|| "search_result_invalid: SearXNG results are missing".to_string())?;
    let now = chrono::Utc::now().to_rfc3339();
    let mut sources = Vec::new();
    for (index, item) in results.iter().take(request.max_results.min(8)).enumerate() {
        let raw_url = item
            .get("url")
            .and_then(|value| value.as_str())
            .unwrap_or_default();
        let parsed = validate_public_page_url(raw_url)?;
        sources.push(WebSource {
            source_id: format!("S{}", index + 1),
            title: item
                .get("title")
                .and_then(|value| value.as_str())
                .unwrap_or(parsed.as_str())
                .chars()
                .take(300)
                .collect(),
            url: parsed.to_string(),
            domain: parsed.host_str().unwrap_or_default().to_string(),
            snippet: item
                .get("content")
                .and_then(|value| value.as_str())
                .map(|text| text.chars().take(2_000).collect()),
            published_at: item
                .get("publishedDate")
                .and_then(|value| value.as_str())
                .map(str::to_string),
            retrieved_at: now.clone(),
            content_hash: None,
        });
    }
    Ok(SearchResultSet {
        sources,
        credits_used: None,
    })
}

pub async fn extract(network: &SafeNetwork, source: WebSource) -> Result<ExtractedPage, String> {
    let url = validate_public_page_url(&source.url)?;
    let (_final_url, raw) = network
        .text(url, Duration::from_secs(PAGE_TIMEOUT_SECS))
        .await?;
    let scripts =
        Regex::new(r"(?is)<(?:script|style|noscript)[^>]*>.*?</(?:script|style|noscript)>")
            .expect("valid regex");
    let tags = Regex::new(r"(?s)<[^>]+>").expect("valid regex");
    let whitespace = Regex::new(r"\s+").expect("valid regex");
    let without_scripts = scripts.replace_all(&raw, " ");
    let plain = tags.replace_all(&without_scripts, " ");
    let content = whitespace.replace_all(&plain, " ").to_string();
    if content.chars().count() > 120_000 {
        return Err(
            "web_content_too_large: Extracted page exceeds the 120,000 character limit".to_string(),
        );
    }
    let mut source = source;
    source.content_hash = Some(format!("{:x}", Sha256::digest(content.as_bytes())));
    Ok(ExtractedPage { source, content })
}
