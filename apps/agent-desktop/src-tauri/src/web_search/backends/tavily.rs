use std::time::Duration;

use reqwest::Method;
use serde_json::json;
use sha2::{Digest, Sha256};
use url::Url;

use crate::web_search::{
    domain::{ExtractedPage, SearchRequest, SearchResultSet, WebSource},
    network::SafeNetwork,
    policy::{validate_public_page_url, PAGE_TIMEOUT_SECS, REQUEST_TIMEOUT_SECS},
};

pub async fn search(
    network: &SafeNetwork,
    base_url: &str,
    key: &str,
    request: &SearchRequest,
) -> Result<SearchResultSet, String> {
    let url = endpoint(base_url, "search")?;
    let value = network
        .json(
            Method::POST,
            url,
            Some(key),
            Some(json!({
                "query": request.query,
                "max_results": request.max_results.min(8),
                "include_answer": false,
                "include_raw_content": false,
                "search_depth": "basic",
                "time_range": request.time_range.as_deref(),
            })),
            Duration::from_secs(REQUEST_TIMEOUT_SECS),
            false,
        )
        .await?;
    let now = chrono::Utc::now().to_rfc3339();
    let results = value
        .get("results")
        .and_then(|item| item.as_array())
        .ok_or_else(|| "search_result_invalid: Tavily results are missing".to_string())?;
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
                .get("published_date")
                .and_then(|value| value.as_str())
                .map(str::to_string),
            retrieved_at: now.clone(),
            content_hash: None,
        });
    }
    Ok(SearchResultSet {
        sources,
        credits_used: value
            .get("usage")
            .and_then(|usage| usage.get("credits"))
            .and_then(|item| item.as_f64()),
    })
}

pub async fn extract(
    network: &SafeNetwork,
    base_url: &str,
    key: &str,
    source: WebSource,
) -> Result<ExtractedPage, String> {
    validate_public_page_url(&source.url)?;
    let value = network
        .json(
            Method::POST,
            endpoint(base_url, "extract")?,
            Some(key),
            Some(json!({"urls": [source.url], "format": "text"})),
            Duration::from_secs(PAGE_TIMEOUT_SECS),
            false,
        )
        .await?;
    let content = value
        .get("results")
        .and_then(|items| items.as_array())
        .and_then(|items| items.first())
        .and_then(|item| item.get("raw_content").or_else(|| item.get("content")))
        .and_then(|value| value.as_str())
        .ok_or_else(|| "search_result_invalid: Tavily extract is missing".to_string())?
        .to_string();
    if content.chars().count() > 120_000 {
        return Err(
            "web_content_too_large: Extracted page exceeds the 120,000 character limit".to_string(),
        );
    }
    let mut source = source;
    source.content_hash = Some(format!("{:x}", Sha256::digest(content.as_bytes())));
    Ok(ExtractedPage { source, content })
}

fn endpoint(base: &str, path: &str) -> Result<Url, String> {
    let base = if base.trim().is_empty() {
        "https://api.tavily.com"
    } else {
        base.trim_end_matches('/')
    };
    Url::parse(&format!("{base}/{path}"))
        .map_err(|_| "search_backend_invalid: Invalid Tavily URL".to_string())
}
