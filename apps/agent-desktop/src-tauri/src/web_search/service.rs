use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};

use crate::{credentials::CredentialStore, settings::SearchBackendSettings};

use super::{
    backends::{searxng, tavily},
    domain::{ExtractedPage, SearchBackendStatus, SearchRequest, SearchResultSet, WebSource},
    network::SafeNetwork,
};

#[derive(Clone, Default)]
pub struct WebSearchService {
    network: SafeNetwork,
    active_runs: Arc<Mutex<HashMap<String, tokio::task::AbortHandle>>>,
}

impl WebSearchService {
    pub fn register_run(&self, run_id: String, handle: tokio::task::AbortHandle) {
        if let Ok(mut active) = self.active_runs.lock() {
            if let Some(previous) = active.insert(run_id, handle) {
                previous.abort();
            }
        }
    }

    pub fn finish_run(&self, run_id: &str) {
        if let Ok(mut active) = self.active_runs.lock() {
            active.remove(run_id);
        }
    }

    pub fn cancel_run(&self, run_id: &str) {
        if let Ok(mut active) = self.active_runs.lock() {
            if let Some(handle) = active.remove(run_id) {
                handle.abort();
            }
        }
    }

    pub async fn test(
        &self,
        backend: &SearchBackendSettings,
        credentials: &CredentialStore,
    ) -> Result<SearchBackendStatus, String> {
        crate::offline::deny_network()?;
        let request = SearchRequest {
            query: "agent connectivity test".to_string(),
            max_results: 1,
            language: "en".to_string(),
            region: "auto".to_string(),
            safe_search: true,
            time_range: None,
        };
        self.search(backend, credentials, &request).await?;
        Ok(SearchBackendStatus {
            available: true,
            message: "ok".to_string(),
            tested_at: chrono::Utc::now().to_rfc3339(),
        })
    }

    pub async fn search(
        &self,
        backend: &SearchBackendSettings,
        credentials: &CredentialStore,
        request: &SearchRequest,
    ) -> Result<SearchResultSet, String> {
        crate::offline::deny_network()?;
        if !backend.enabled {
            return Err("search_backend_not_configured: Search backend is disabled".to_string());
        }
        let key = if backend.kind == "tavily" {
            Some(
                credentials
                    .get(&format!("web-search:{}", backend.id))?
                    .ok_or_else(|| {
                        "search_authentication_failed: Tavily key is missing".to_string()
                    })?,
            )
        } else {
            None
        };
        for attempt in 0..=1 {
            let result = match backend.kind.as_str() {
                "tavily" => {
                    tavily::search(
                        &self.network,
                        &backend.base_url,
                        key.as_deref().unwrap_or_default(),
                        request,
                    )
                    .await
                }
                "searxng" => searxng::search(&self.network, &backend.base_url, request).await,
                _ => {
                    return Err(
                        "search_backend_not_configured: Unsupported search backend".to_string()
                    )
                }
            };
            match result {
                Ok(value) => return Ok(value),
                Err(error) if attempt == 0 && retryable_before_results(&error) => {
                    tokio::time::sleep(std::time::Duration::from_millis(650)).await;
                }
                Err(error)
                    if attempt == 0 && retry_after(&error).is_some_and(|value| value <= 30) =>
                {
                    tokio::time::sleep(std::time::Duration::from_secs(
                        retry_after(&error).unwrap_or(0),
                    ))
                    .await;
                }
                Err(error) => return Err(error),
            }
        }
        unreachable!("bounded search retry loop always returns")
    }

    pub async fn extract(
        &self,
        backend: &SearchBackendSettings,
        credentials: &CredentialStore,
        source: WebSource,
    ) -> Result<ExtractedPage, String> {
        crate::offline::deny_network()?;
        match backend.kind.as_str() {
            "tavily" => {
                let key = credentials
                    .get(&format!("web-search:{}", backend.id))?
                    .ok_or_else(|| {
                        "search_authentication_failed: Tavily key is missing".to_string()
                    })?;
                tavily::extract(&self.network, &backend.base_url, &key, source).await
            }
            "searxng" => searxng::extract(&self.network, source).await,
            _ => Err("search_backend_not_configured: Unsupported search backend".to_string()),
        }
    }
}

fn retryable_before_results(error: &str) -> bool {
    error.starts_with("network_unavailable:") || error.starts_with("connection_reset:")
}

fn retry_after(error: &str) -> Option<u64> {
    error
        .split("retry_after=")
        .nth(1)?
        .split_whitespace()
        .next()?
        .parse()
        .ok()
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;

    #[tokio::test]
    async fn cancelling_a_run_aborts_its_inflight_network_task() {
        let service = WebSearchService::default();
        let task = tokio::spawn(async {
            tokio::time::sleep(Duration::from_secs(30)).await;
        });
        service.register_run("run-1".to_string(), task.abort_handle());

        service.cancel_run("run-1");

        assert!(task.await.unwrap_err().is_cancelled());
    }
}
