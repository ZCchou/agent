use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::Mutex,
};
use tauri::{AppHandle, State};

const STORE_VERSION: u32 = 2;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RouteHealthKey {
    pub target_kind: String,
    pub provider_or_backend_id: String,
    #[serde(default)]
    pub model_id: Option<String>,
    pub route_id: String,
    #[serde(default)]
    pub capability_signature: Vec<String>,
    pub route_fingerprint: String,
    #[serde(default = "default_tool_transport")]
    pub tool_transport: String,
    #[serde(default)]
    pub tool_contract_version: String,
    #[serde(default)]
    pub tool_schema_hash: String,
}

fn default_tool_transport() -> String {
    "none".to_string()
}

impl RouteHealthKey {
    pub fn normalize(&mut self) {
        self.capability_signature.sort();
        self.capability_signature.dedup();
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RouteHealthRecord {
    #[serde(flatten)]
    pub key: RouteHealthKey,
    pub health_status: String,
    pub consecutive_failures: u32,
    #[serde(default)]
    pub last_error_code: Option<String>,
    #[serde(default)]
    pub last_failure_at: Option<String>,
    #[serde(default)]
    pub retry_at: Option<String>,
    #[serde(default)]
    pub blocked_config_generation: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct RouteHealthFile {
    version: u32,
    #[serde(default)]
    last_observed_at: Option<String>,
    records: Vec<RouteHealthRecord>,
}

impl Default for RouteHealthFile {
    fn default() -> Self {
        Self {
            version: STORE_VERSION,
            last_observed_at: None,
            records: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutcomeClass {
    ExplicitlyUnsupported,
    Temporary,
    RateLimited,
    QuotaExhausted,
    ConfigurationBlocked,
    RequestScoped,
    Cancelled,
}

pub fn classify_error(error_code: &str) -> OutcomeClass {
    match error_code {
        "model_vision_unsupported"
        | "multimodal_protocol_unsupported"
        | "native_search_protocol_unsupported"
        | "capability_signature_unsupported"
        | "capability_unsupported"
        | "capability_verification_failed" => OutcomeClass::ExplicitlyUnsupported,
        "rate_limited" | "search_rate_limited" | "provider_rate_limited" => {
            OutcomeClass::RateLimited
        }
        "quota_exhausted" | "resource_exhausted" | "search_quota_exhausted" => {
            OutcomeClass::QuotaExhausted
        }
        "authentication_failed"
        | "credential_invalid"
        | "route_not_configured"
        | "search_authentication_failed"
        | "provider_model_not_found"
        | "model_not_found"
        | "invalid_provider"
        | "invalid_provider_url"
        | "insecure_provider_url"
        | "invalid_provider_response"
        | "provider_invalid_response"
        | "provider_endpoint_invalid"
        | "native_search_continuation_unsupported"
        | "invocation_protocol_mismatch"
        | "provider_event_unadapted" => OutcomeClass::ConfigurationBlocked,
        "content_policy_rejected"
        | "provider_refusal"
        | "image_format_unsupported"
        | "image_payload_too_large"
        | "current_input_too_large"
        | "context_window_exceeded"
        | "tool_not_allowed"
        | "tool_execution_failed"
        | "approval_rejected"
        | "attachment_processing_failed" => OutcomeClass::RequestScoped,
        "evidence_missing" => OutcomeClass::RequestScoped,
        "cancelled" | "cancelled_by_user" | "run_cancelled" | "core_interrupted" => {
            OutcomeClass::Cancelled
        }
        _ => OutcomeClass::Temporary,
    }
}

pub struct RouteHealthRegistry {
    path: PathBuf,
    data: RouteHealthFile,
}

impl RouteHealthRegistry {
    pub fn load(path: PathBuf) -> Result<Self, String> {
        let source_path = if path.exists() {
            path.clone()
        } else {
            let legacy = path.with_file_name("route-health-v1.json");
            if legacy.exists() {
                legacy
            } else {
                path.clone()
            }
        };
        if !source_path.exists() {
            return Ok(Self {
                path,
                data: RouteHealthFile::default(),
            });
        }
        let loaded = fs::read_to_string(&source_path).ok().and_then(|text| {
            if let Ok(value) = serde_json::from_str::<RouteHealthFile>(&text) {
                if value.version == STORE_VERSION {
                    return Some(value);
                }
            }
            #[derive(Deserialize)]
            struct LegacyRouteHealthFile {
                version: u32,
                records: Vec<RouteHealthRecord>,
            }
            let legacy = serde_json::from_str::<LegacyRouteHealthFile>(&text).ok()?;
            if legacy.version != 1 {
                return None;
            }
            Some(RouteHealthFile {
                version: STORE_VERSION,
                last_observed_at: None,
                records: legacy.records,
            })
        });
        if let Some(data) = loaded {
            return Ok(Self { path, data });
        }
        let damaged = source_path.with_file_name(format!(
            "route-health-v2-damaged-{}.json",
            uuid::Uuid::new_v4()
        ));
        eprintln!(
            "[agent] Route health registry was invalid and has been isolated; routes default to ready."
        );
        let _ = fs::rename(&path, damaged);
        Ok(Self {
            path,
            data: RouteHealthFile::default(),
        })
    }

    pub fn snapshot(&self) -> Vec<RouteHealthRecord> {
        let now = observed_now(&self.data);
        self.data
            .records
            .iter()
            .cloned()
            .map(|mut record| {
                if record.health_status == "cooling_down" && retry_has_elapsed(&record, now) {
                    record.health_status = "degraded".to_string();
                    record.retry_at = None;
                }
                record
            })
            .collect()
    }

    pub fn record_success(&mut self, mut key: RouteHealthKey) -> Result<(), String> {
        key.normalize();
        self.observe_clock();
        self.upsert(RouteHealthRecord {
            key,
            health_status: "ready".to_string(),
            consecutive_failures: 0,
            last_error_code: None,
            last_failure_at: None,
            retry_at: None,
            blocked_config_generation: None,
        })
    }

    pub fn record_failure(
        &mut self,
        mut key: RouteHealthKey,
        error_code: &str,
        retry_after_seconds: Option<u64>,
        config_generation: Option<u64>,
    ) -> Result<OutcomeClass, String> {
        key.normalize();
        let class = classify_error(error_code);
        if matches!(
            class,
            OutcomeClass::ExplicitlyUnsupported
                | OutcomeClass::RequestScoped
                | OutcomeClass::Cancelled
        ) {
            return Ok(class);
        }
        let prior = self
            .data
            .records
            .iter()
            .find(|record| same_key(&record.key, &key))
            .map_or(0, |record| record.consecutive_failures);
        let failures = prior.saturating_add(1);
        let now = self.observe_clock();
        let (status, retry_at, blocked_generation) = match class {
            OutcomeClass::ConfigurationBlocked => {
                ("configuration_blocked", None, config_generation)
            }
            OutcomeClass::RateLimited => (
                "cooling_down",
                Some(now + Duration::seconds(retry_after_seconds.unwrap_or(60) as i64)),
                None,
            ),
            OutcomeClass::QuotaExhausted => {
                ("cooling_down", Some(now + Duration::minutes(15)), None)
            }
            _ if failures >= 3 => {
                let seconds = if failures == 3 {
                    60
                } else if failures == 4 {
                    300
                } else {
                    900
                };
                ("cooling_down", Some(now + Duration::seconds(seconds)), None)
            }
            _ => ("degraded", None, None),
        };
        self.upsert(RouteHealthRecord {
            key,
            health_status: status.to_string(),
            consecutive_failures: failures,
            last_error_code: Some(error_code.to_string()),
            last_failure_at: Some(now.to_rfc3339()),
            retry_at: retry_at.map(|value| value.to_rfc3339()),
            blocked_config_generation: blocked_generation,
        })?;
        Ok(class)
    }

    pub fn reset(&mut self, key: &RouteHealthKey) -> Result<(), String> {
        let previous = self.data.clone();
        let before = self.data.records.len();
        self.data
            .records
            .retain(|record| !same_key(&record.key, key));
        if before != self.data.records.len() {
            if let Err(error) = self.persist() {
                self.data = previous;
                return Err(error);
            }
        }
        Ok(())
    }

    pub fn clear_configuration_blocks(&mut self, provider_id: &str) -> Result<(), String> {
        let previous = self.data.clone();
        let before = self.data.records.len();
        self.data.records.retain(|record| {
            !(record.key.provider_or_backend_id == provider_id
                && record.health_status == "configuration_blocked")
        });
        if before != self.data.records.len() {
            if let Err(error) = self.persist() {
                self.data = previous;
                return Err(error);
            }
        }
        Ok(())
    }

    pub fn clear_target(&mut self, target_kind: &str, target_id: &str) -> Result<(), String> {
        let previous = self.data.clone();
        let before = self.data.records.len();
        self.data.records.retain(|record| {
            record.key.target_kind != target_kind || record.key.provider_or_backend_id != target_id
        });
        if before != self.data.records.len() {
            if let Err(error) = self.persist() {
                self.data = previous;
                return Err(error);
            }
        }
        Ok(())
    }

    fn upsert(&mut self, record: RouteHealthRecord) -> Result<(), String> {
        let previous = self.data.clone();
        self.data
            .records
            .retain(|item| !same_key(&item.key, &record.key));
        self.data.records.push(record);
        if let Err(error) = self.persist() {
            self.data = previous;
            return Err(error);
        }
        Ok(())
    }

    fn observe_clock(&mut self) -> DateTime<Utc> {
        let now = observed_now(&self.data);
        self.data.last_observed_at = Some(now.to_rfc3339());
        now
    }

    fn persist(&self) -> Result<(), String> {
        let parent = self
            .path
            .parent()
            .ok_or_else(|| "route health path is invalid".to_string())?;
        fs::create_dir_all(parent)
            .map_err(|error| format!("route health directory failed: {error}"))?;
        let text = serde_json::to_vec_pretty(&self.data)
            .map_err(|error| format!("route health serialization failed: {error}"))?;
        let temporary = parent.join(format!("route-health-{}.tmp", uuid::Uuid::new_v4()));
        let mut file = fs::File::create(&temporary)
            .map_err(|error| format!("route health write failed: {error}"))?;
        file.write_all(&text)
            .and_then(|_| file.sync_all())
            .map_err(|error| format!("route health write failed: {error}"))?;
        replace_file(&temporary, &self.path)
    }
}

fn observed_now(data: &RouteHealthFile) -> DateTime<Utc> {
    let wall = Utc::now();
    data.last_observed_at
        .as_deref()
        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
        .map(|value| value.with_timezone(&Utc))
        .map_or(wall, |observed| observed.max(wall))
}

fn same_key(left: &RouteHealthKey, right: &RouteHealthKey) -> bool {
    left == right
}

fn retry_has_elapsed(record: &RouteHealthRecord, now: DateTime<Utc>) -> bool {
    record
        .retry_at
        .as_deref()
        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
        .is_none_or(|value| value.with_timezone(&Utc) <= now)
}

fn replace_file(source: &Path, target: &Path) -> Result<(), String> {
    if !target.exists() {
        return fs::rename(source, target)
            .map_err(|error| format!("route health replace failed: {error}"));
    }
    let parent = target
        .parent()
        .ok_or_else(|| "route health path is invalid".to_string())?;
    let backup = parent.join(format!("route-health-{}.backup", uuid::Uuid::new_v4()));
    fs::rename(target, &backup).map_err(|error| format!("route health backup failed: {error}"))?;
    if let Err(error) = fs::rename(source, target) {
        let _ = fs::rename(&backup, target);
        return Err(format!("route health replace failed: {error}"));
    }
    let _ = fs::remove_file(backup);
    Ok(())
}

#[tauri::command]
pub fn list_route_health(
    registry: State<'_, Mutex<RouteHealthRegistry>>,
) -> Result<Vec<RouteHealthRecord>, String> {
    registry
        .lock()
        .map_err(|_| "route health registry lock failed".to_string())
        .map(|value| value.snapshot())
}

#[tauri::command]
pub async fn reset_route_health(
    app: AppHandle,
    key: RouteHealthKey,
    registry: State<'_, Mutex<RouteHealthRegistry>>,
) -> Result<Vec<RouteHealthRecord>, String> {
    let snapshot = {
        let mut registry = registry
            .lock()
            .map_err(|_| "route health registry lock failed".to_string())?;
        registry.reset(&key)?;
        registry.snapshot()
    };
    crate::settings::commands::reinject_providers(&app).await?;
    Ok(snapshot)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key() -> RouteHealthKey {
        RouteHealthKey {
            target_kind: "model".to_string(),
            provider_or_backend_id: "p1".to_string(),
            model_id: Some("m1".to_string()),
            route_id: "r1".to_string(),
            capability_signature: vec!["vision".to_string(), "text".to_string()],
            route_fingerprint: "fp1".to_string(),
            tool_transport: "none".to_string(),
            tool_contract_version: String::new(),
            tool_schema_hash: String::new(),
        }
    }

    #[test]
    fn temporary_failures_degrade_then_cool_down() {
        let path = std::env::temp_dir().join(format!("route-health-{}.json", uuid::Uuid::new_v4()));
        let mut registry = RouteHealthRegistry::load(path.clone()).unwrap();
        registry
            .record_failure(key(), "provider_empty_response", None, None)
            .unwrap();
        assert_eq!(registry.snapshot()[0].health_status, "degraded");
        registry
            .record_failure(key(), "request_timeout", None, None)
            .unwrap();
        assert_eq!(registry.snapshot()[0].health_status, "degraded");
        registry
            .record_failure(key(), "request_timeout", None, None)
            .unwrap();
        assert_eq!(registry.snapshot()[0].health_status, "cooling_down");
        registry.record_success(key()).unwrap();
        assert_eq!(registry.snapshot()[0].health_status, "ready");
        assert_eq!(registry.snapshot()[0].consecutive_failures, 0);
        let _ = fs::remove_file(path);
    }

    #[test]
    fn request_scoped_failure_does_not_create_health_record() {
        let path = std::env::temp_dir().join(format!("route-health-{}.json", uuid::Uuid::new_v4()));
        let mut registry = RouteHealthRegistry::load(path.clone()).unwrap();
        let class = registry
            .record_failure(key(), "image_format_unsupported", None, None)
            .unwrap();
        assert_eq!(class, OutcomeClass::RequestScoped);
        assert!(registry.snapshot().is_empty());
        let _ = fs::remove_file(path);
    }

    #[test]
    fn damaged_file_fails_open() {
        let path = std::env::temp_dir().join(format!("route-health-{}.json", uuid::Uuid::new_v4()));
        fs::write(&path, "not json").unwrap();
        let registry = RouteHealthRegistry::load(path.clone()).unwrap();
        assert!(registry.snapshot().is_empty());
        assert!(!path.exists());
    }

    #[test]
    fn classifier_keeps_request_failures_out_of_route_health() {
        assert_eq!(
            classify_error("content_policy_rejected"),
            OutcomeClass::RequestScoped
        );
        assert_eq!(
            classify_error("image_payload_too_large"),
            OutcomeClass::RequestScoped
        );
        assert_eq!(
            classify_error("tool_execution_failed"),
            OutcomeClass::RequestScoped
        );
        assert_eq!(classify_error("cancelled_by_user"), OutcomeClass::Cancelled);
        assert_eq!(classify_error("core_interrupted"), OutcomeClass::Cancelled);
        assert_eq!(
            classify_error("authentication_failed"),
            OutcomeClass::ConfigurationBlocked
        );
        assert_eq!(classify_error("rate_limited"), OutcomeClass::RateLimited);
        assert_eq!(
            classify_error("quota_exhausted"),
            OutcomeClass::QuotaExhausted
        );
        assert_eq!(
            classify_error("model_vision_unsupported"),
            OutcomeClass::ExplicitlyUnsupported
        );
    }

    #[test]
    fn arbitrary_future_capability_signatures_share_the_same_health_policy() {
        let path = std::env::temp_dir().join(format!("route-health-{}.json", uuid::Uuid::new_v4()));
        let mut registry = RouteHealthRegistry::load(path.clone()).unwrap();
        for index in 0..1_000 {
            let mut item = key();
            item.route_id = format!("future-route-{index}");
            item.route_fingerprint = format!("future-fingerprint-{index}");
            item.capability_signature = vec!["text".to_string(), format!("future_{index}")];
            registry
                .record_failure(item, "provider_empty_response", None, None)
                .unwrap();
        }
        let snapshot = registry.snapshot();
        assert_eq!(snapshot.len(), 1_000);
        assert!(snapshot.iter().all(|record| {
            record.health_status == "degraded" && record.consecutive_failures == 1
        }));
        let _ = fs::remove_file(path);
    }
}
