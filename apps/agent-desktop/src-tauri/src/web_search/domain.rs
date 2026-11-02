use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchRequest {
    pub query: String,
    pub max_results: usize,
    pub language: String,
    pub region: String,
    pub safe_search: bool,
    pub time_range: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WebSource {
    pub source_id: String,
    pub title: String,
    pub url: String,
    pub domain: String,
    pub snippet: Option<String>,
    pub published_at: Option<String>,
    pub retrieved_at: String,
    pub content_hash: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SearchResultSet {
    pub sources: Vec<WebSource>,
    pub credits_used: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExtractedPage {
    pub source: WebSource,
    pub content: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SearchBackendStatus {
    pub available: bool,
    pub message: String,
    pub tested_at: String,
}

#[cfg(test)]
mod tests {
    #[test]
    fn shared_contract_contains_the_host_search_states() {
        let contract: serde_json::Value =
            serde_json::from_str(include_str!("../../../../../contracts/web-search-v2.json"))
                .unwrap();
        assert_eq!(contract["policy_version"], "web-search-policy-v2");
        assert!(contract["operation_statuses"]
            .as_array()
            .unwrap()
            .iter()
            .any(|value| value == "unknown_outcome"));
        assert!(contract["routes"]
            .as_array()
            .unwrap()
            .iter()
            .any(|value| value == "host_presearch"));
    }
}
