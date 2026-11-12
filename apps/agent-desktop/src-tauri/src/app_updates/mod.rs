pub mod draft;
mod recovery;

use crate::{core_bridge::request_core, core_process_manager::CoreProcessManager};
use base64::Engine;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{sync::Mutex, time::Duration};
use tauri::{AppHandle, Emitter, Manager, State};
use tauri_plugin_updater::{Update, UpdaterExt};

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Channel {
    #[default]
    Preview,
    Stable,
}
impl Channel {
    fn endpoint(self) -> &'static str {
        match self {
            Self::Preview => {
                "https://raw.githubusercontent.com/ZCchou/agent/main/updates/airworthiness-preview.json"
            }
            Self::Stable => {
                "https://raw.githubusercontent.com/ZCchou/agent/main/updates/airworthiness-stable.json"
            }
        }
    }
}

#[derive(Clone, Serialize)]
pub struct UpdateView {
    state: String,
    current_version: String,
    channel: Channel,
    update_id: Option<String>,
    version: Option<String>,
    notes: Option<String>,
    downloaded_bytes: u64,
    total_bytes: Option<u64>,
    error_code: Option<String>,
}
impl Default for UpdateView {
    fn default() -> Self {
        Self {
            state: "idle".into(),
            current_version: env!("CARGO_PKG_VERSION").into(),
            channel: Channel::Preview,
            update_id: None,
            version: None,
            notes: None,
            downloaded_bytes: 0,
            total_bytes: None,
            error_code: None,
        }
    }
}
#[derive(Default)]
struct CachedUpdate {
    view: UpdateView,
    update: Option<Update>,
    bytes: Option<Vec<u8>>,
}
impl CachedUpdate {
    fn busy(&self) -> bool {
        matches!(
            self.view.state.as_str(),
            "checking" | "downloading" | "installing"
        )
    }
    fn check_identity(&self, id: &str) -> Result<(), String> {
        if self.view.update_id.as_deref() != Some(id) {
            return Err("app_update_stale".into());
        }
        if self.busy() {
            return Err("app_update_busy".into());
        }
        Ok(())
    }
}
#[derive(Default)]
pub struct AppUpdateService {
    inner: Mutex<CachedUpdate>,
}

fn emit(app: &AppHandle, view: &UpdateView) {
    let _ = app.emit("app-update-progress", view);
}
fn error_code(error: &tauri_plugin_updater::Error) -> &'static str {
    use tauri_plugin_updater::Error;
    match error {
        Error::Minisign(_)
        | Error::Base64(_)
        | Error::SignatureUtf8(_)
        | Error::SignedVersionMismatch { .. }
        | Error::MissingSignedVersion => "app_update_signature_invalid",
        Error::Reqwest(_) | Error::Network(_) => "app_update_network",
        _ => "app_update_feed_invalid",
    }
}
fn accepted_download_url(url: &url::Url) -> bool {
    url.scheme() == "https"
        && url.host_str() == Some("github.com")
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none()
        && url.fragment().is_none()
        && url.port_or_known_default() == Some(443)
        && url.path().starts_with("/ZCchou/agent/releases/download/")
        && !url.path().ends_with('/')
}

fn program_redirect_url(url: &url::Url) -> bool {
    url.scheme() == "https"
        && url.port_or_known_default() == Some(443)
        && url.username().is_empty()
        && url.password().is_none()
        && url.fragment().is_none()
        && matches!(
            url.host_str(),
            Some(
                "github.com"
                    | "release-assets.githubusercontent.com"
                    | "objects.githubusercontent.com"
            )
        )
}

async fn download_signed_program(
    app: &AppHandle,
    update: &Update,
    update_id: &str,
) -> Result<Vec<u8>, String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(900))
        .redirect(reqwest::redirect::Policy::custom(|attempt| {
            if attempt.previous().len() < 5 && program_redirect_url(attempt.url()) {
                attempt.follow()
            } else {
                attempt.error("Program download left the allowed release hosts")
            }
        }))
        .build()
        .map_err(|_| "app_update_network")?;
    let mut response = client
        .get(update.download_url.clone())
        .send()
        .await
        .map_err(|_| "app_update_network")?;
    if !response.status().is_success() {
        return Err("app_update_network".into());
    }
    let total = response.content_length();
    let limit = 512 * 1024 * 1024;
    if total.is_some_and(|size| size > limit as u64) {
        return Err("app_update_package_too_large".into());
    }
    let mut bytes = Vec::new();
    let mut last_sent = 0;
    while let Some(chunk) = response.chunk().await.map_err(|_| "app_update_network")? {
        if bytes.len().saturating_add(chunk.len()) > limit {
            return Err("app_update_package_too_large".into());
        }
        bytes.extend_from_slice(&chunk);
        if bytes.len().saturating_sub(last_sent) >= 128 * 1024 {
            last_sent = bytes.len();
            let state = app.state::<AppUpdateService>();
            let mut cache = state.inner.lock().map_err(|_| "app_update_not_ready")?;
            if cache.view.update_id.as_deref() != Some(update_id) {
                return Err("app_update_stale".into());
            }
            cache.view.downloaded_bytes = bytes.len() as u64;
            cache.view.total_bytes = total;
            emit(app, &cache.view);
        }
    }
    crate::program_update::verify_signed_bytes(
        &bytes,
        update.signature.as_bytes(),
        include_str!("../../updater-public-key.txt"),
    )?;
    Ok(bytes)
}

fn matches_channel(url: &url::Url, channel: Channel, version: &str) -> bool {
    if !accepted_download_url(url) {
        return false;
    }
    let segments = url
        .path_segments()
        .map(|parts| parts.collect::<Vec<_>>())
        .unwrap_or_default();
    if segments.len() != 6
        || segments[5] != format!("agent-{version}-windows-x86_64-program.zip")
        || segments[4] != format!("v{version}")
    {
        return false;
    }
    match channel {
        Channel::Stable => !version.contains('-'),
        Channel::Preview => version
            .split_once("-preview.")
            .is_some_and(|(base, serial)| {
                !base.contains('-')
                    && !serial.is_empty()
                    && !serial.starts_with('0')
                    && serial.bytes().all(|c| c.is_ascii_digit())
            }),
    }
}

fn verify_feed(
    bytes: &[u8],
    signature: &[u8],
    public_key: &str,
) -> Result<serde_json::Value, &'static str> {
    let decode = |value: &[u8]| -> Result<String, &'static str> {
        let raw = base64::engine::general_purpose::STANDARD
            .decode(
                value
                    .iter()
                    .copied()
                    .filter(|c| !c.is_ascii_whitespace())
                    .collect::<Vec<_>>(),
            )
            .map_err(|_| "app_update_signature_invalid")?;
        String::from_utf8(raw).map_err(|_| "app_update_signature_invalid")
    };
    let key = minisign_verify::PublicKey::decode(&decode(public_key.as_bytes())?)
        .map_err(|_| "app_update_signature_invalid")?;
    let signature = minisign_verify::Signature::decode(&decode(signature)?)
        .map_err(|_| "app_update_signature_invalid")?;
    key.verify(bytes, &signature, true)
        .map_err(|_| "app_update_signature_invalid")?;
    serde_json::from_slice(bytes).map_err(|_| "app_update_feed_invalid")
}

async fn bounded_body(
    mut response: reqwest::Response,
    limit: usize,
) -> Result<Vec<u8>, &'static str> {
    if response
        .content_length()
        .is_some_and(|len| len > limit as u64)
    {
        return Err("app_update_feed_invalid");
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|_| "app_update_network")? {
        if bytes.len().saturating_add(chunk.len()) > limit {
            return Err("app_update_feed_invalid");
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}

#[tauri::command]
pub fn app_update_status(service: State<'_, AppUpdateService>) -> Result<UpdateView, String> {
    let mut cache = service.inner.lock().map_err(|_| "app_update_not_ready")?;
    if cache.view.state == "idle" {
        if let Some(error) = crate::program_update_session::previous_update_error() {
            cache.view.state = "error".into();
            cache.view.error_code = Some(error);
        }
    }
    Ok(cache.view.clone())
}

#[tauri::command]
pub async fn check_app_update(
    app: AppHandle,
    service: State<'_, AppUpdateService>,
    channel: Channel,
) -> Result<UpdateView, String> {
    // An independent release signing identity must be configured before any network access.
    if app
        .config()
        .plugins
        .0
        .get("updater")
        .and_then(|config| config.get("pubkey"))
        .and_then(serde_json::Value::as_str)
        .is_none_or(|key| key.trim().is_empty())
    {
        return Err("app_update_not_ready".into());
    }
    {
        let mut cache = service.inner.lock().map_err(|_| "app_update_not_ready")?;
        if cache.busy() {
            return Err("app_update_busy".into());
        }
        *cache = CachedUpdate::default();
        cache.view.state = "checking".into();
        cache.view.channel = channel;
        cache.view.update_id = Some(uuid::Uuid::new_v4().to_string());
        emit(&app, &cache.view);
    }
    let outcome = async {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|_| "app_update_network")?;
        let response = client
            .get(channel.endpoint())
            .send()
            .await
            .map_err(|_| "app_update_network")?;
        if response.status() == reqwest::StatusCode::NOT_FOUND {
            return Ok(None);
        }
        if !response.status().is_success() {
            return Err("app_update_network");
        }
        let bytes = bounded_body(response, 1024 * 1024).await?;
        let response = client
            .get(format!("{}.sig", channel.endpoint()))
            .send()
            .await
            .map_err(|_| "app_update_network")?;
        if !response.status().is_success() {
            return Err("app_update_signature_invalid");
        }
        let signature = bounded_body(response, 16 * 1024).await?;
        let config = app.config();
        let public_key = config
            .plugins
            .0
            .get("updater")
            .and_then(|config| config.get("pubkey"))
            .and_then(serde_json::Value::as_str)
            .ok_or("app_update_signature_invalid")?;
        let verified_feed = verify_feed(&bytes, &signature, public_key)?;
        let updater = app
            .updater_builder()
            // The default Tauri hook hides windows and clears resources BEFORE
            // ShellExecuteW can fail. Core is already flushed/stopped by our
            // install command; retain the app until the plugin confirms the
            // handoff and exits, so failure can return to the live chat.
            .on_before_exit(|| {})
            .endpoints(vec![channel
                .endpoint()
                .parse()
                .map_err(|_| "app_update_feed_invalid")?])
            .map_err(|_| "app_update_feed_invalid")?
            .timeout(Duration::from_secs(30))
            .build()
            .map_err(|_| "app_update_feed_invalid")?;
        let update = updater.check().await.map_err(|error| error_code(&error))?;
        if let Some(ref update) = update {
            if update.raw_json != verified_feed {
                return Err("app_update_signature_invalid");
            }
            if !matches_channel(&update.download_url, channel, &update.version) {
                return Err("app_update_feed_invalid");
            }
        }
        Ok(Some(update))
    }
    .await;
    let mut cache = service.inner.lock().map_err(|_| "app_update_not_ready")?;
    match outcome {
        Ok(None) => {
            cache.view.state = "channel_unavailable".into();
        }
        Ok(Some(None)) => {
            cache.view.state = "no_update".into();
        }
        Ok(Some(Some(mut update))) => {
            update.timeout = Some(Duration::from_secs(900));
            cache.view.state = "available".into();
            cache.view.version = Some(update.version.clone());
            cache.view.notes = update.body.clone();
            cache.update = Some(update);
        }
        Err(code) => {
            cache.view.state = "error".into();
            cache.view.error_code = Some(code.into());
        }
    }
    emit(&app, &cache.view);
    Ok(cache.view.clone())
}

#[tauri::command]
pub async fn download_app_update(
    app: AppHandle,
    service: State<'_, AppUpdateService>,
    update_id: String,
) -> Result<UpdateView, String> {
    let update = {
        let mut cache = service.inner.lock().map_err(|_| "app_update_not_ready")?;
        cache.check_identity(&update_id)?;
        let update = cache.update.clone().ok_or("app_update_not_ready")?;
        cache.bytes = None;
        cache.view.state = "downloading".into();
        cache.view.error_code = None;
        cache.view.downloaded_bytes = 0;
        cache.view.total_bytes = None;
        emit(&app, &cache.view);
        update
    };
    let result = download_signed_program(&app, &update, &update_id).await;
    let mut cache = service.inner.lock().map_err(|_| "app_update_not_ready")?;
    match result {
        Ok(bytes) => {
            cache.view.state = "ready".into();
            cache.view.downloaded_bytes = bytes.len() as u64;
            cache.view.total_bytes = Some(bytes.len() as u64);
            cache.bytes = Some(bytes);
        }
        Err(error) => {
            cache.view.state = "error".into();
            cache.view.error_code = Some(error);
            cache.bytes = None;
        }
    }
    emit(&app, &cache.view);
    Ok(cache.view.clone())
}

#[tauri::command]
pub async fn install_app_update(
    app: AppHandle,
    service: State<'_, AppUpdateService>,
    manager: State<'_, Mutex<CoreProcessManager>>,
    update_id: String,
    draft: Option<draft::RestartDraft>,
) -> Result<(), String> {
    if crate::profile_migration::is_busy(&app) {
        return Err("app_update_busy".into());
    }
    let (update, mut bytes) = {
        let mut cache = service.inner.lock().map_err(|_| "app_update_not_ready")?;
        cache.check_identity(&update_id)?;
        let update = cache.update.clone().ok_or("app_update_not_ready")?;
        let bytes = cache.bytes.take().ok_or("app_update_not_ready")?;
        cache.view.state = "installing".into();
        cache.view.error_code = None;
        emit(&app, &cache.view);
        (update, bytes)
    };
    let mut core_shutdown_started = false;
    let mut core_prepared = false;
    let mut checkpoint_saved = false;
    let result: Result<(), String> = async {
        let prepared = request_core(
            &app,
            &manager,
            "app.update.prepare",
            json!({"update_id":update_id}),
        )
        .await
        .map_err(|_| "app_update_core_unavailable")?;
        if prepared.get("ready").and_then(serde_json::Value::as_bool) != Some(true) {
            return Err("app_update_busy".into());
        }
        core_prepared = true;
        draft::save_for_app(&app, &update_id, draft)?;
        checkpoint_saved = true;
        core_shutdown_started = true;
        manager
            .lock()
            .map_err(|_| "app_update_core_unavailable")?
            .shutdown_for_update()
            .map_err(|_| "app_update_core_unavailable")?;
        let root = app
            .path()
            .app_data_dir()
            .map_err(|_| "app_update_install_failed")?;
        let payload = std::mem::take(&mut bytes);
        let signature = update.signature.clone();
        let version = update.version.clone();
        let id = update_id.clone();
        let (result, returned) = tauri::async_runtime::spawn_blocking(move || {
            let result = crate::program_update_session::prepare_and_handoff(
                &root, &payload, &signature, &version, &id,
            );
            (result, payload)
        })
        .await
        .map_err(|_| "app_update_handoff_failed")?;
        bytes = returned;
        result?;
        app.exit(0);
        Ok(())
    }
    .await;
    if let Err(error) = result {
        // Release the Core admission closure even if persistence/installation fails.
        if checkpoint_saved {
            let _ = draft::remove_for_app(&app, &update_id);
        }
        recovery::recover(
            core_shutdown_started,
            core_prepared,
            || async {
                // A lost prepare ACK can still mean the Core closed admissions.
                // Matching abort is safe even when preparation was unconfirmed.
                request_core(
                    &app,
                    &manager,
                    "app.update.abort",
                    json!({"update_id":update_id}),
                )
                .await
                .map(|_| ())
            },
            || {
                // Restart only after the Core was proven idle, never on an
                // unconfirmed prepare that might have refused active work.
                if let Ok(mut manager) = manager.lock() {
                    let _ = manager.shutdown();
                    let _ = manager.start(&app);
                }
            },
        )
        .await;
        let mut cache = service.inner.lock().map_err(|_| "app_update_not_ready")?;
        cache.bytes = if bytes.is_empty() { None } else { Some(bytes) };
        cache.view.state = if cache.bytes.is_some() {
            "ready"
        } else {
            "available"
        }
        .into();
        cache.view.error_code = Some(error.clone());
        emit(&app, &cache.view);
        return Err(error);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn program_download_redirects_stay_with_public_github_asset_hosts() {
        for value in [
            "https://github.com/ZCchou/agent/releases/download/v0.16.0-preview.1/file.zip",
            "https://release-assets.githubusercontent.com/assets/file?signature=value",
            "https://objects.githubusercontent.com/assets/file",
        ] {
            assert!(program_redirect_url(&value.parse().unwrap()));
        }
        for value in [
            "http://github.com/file",
            "https://evil.test/file",
            "https://github.com.evil.test/file",
            "https://user@github.com/file",
            "https://127.0.0.1/file",
            "https://github.com:444/file",
        ] {
            assert!(!program_redirect_url(&value.parse().unwrap()));
        }
    }
    #[test]
    fn unified_feed_accepts_only_matching_program_zip_and_channel() {
        let version = "0.16.0-preview.1";
        let url: url::Url = format!("https://github.com/ZCchou/agent/releases/download/v{version}/agent-{version}-windows-x86_64-program.zip").parse().unwrap();
        assert!(matches_channel(&url, Channel::Preview, version));
        assert!(!matches_channel(&url, Channel::Stable, version));
        assert!(!matches_channel(&url, Channel::Preview, "0.16.0-preview.2"));
        assert!(!accepted_download_url(
            &format!("{url}?file=other").parse().unwrap()
        ));
        assert!(Channel::Preview
            .endpoint()
            .ends_with("/airworthiness-preview.json"));
    }
    #[test]
    fn signed_release_urls_must_match_channel_version_and_program_package() {
        let preview: url::Url="https://github.com/ZCchou/agent/releases/download/v0.14.2-preview.1/agent-0.14.2-preview.1-windows-x86_64-program.zip".parse().unwrap();
        assert!(matches_channel(
            &preview,
            Channel::Preview,
            "0.14.2-preview.1"
        ));
        assert!(!matches_channel(
            &preview,
            Channel::Stable,
            "0.14.2-preview.1"
        ));
        assert!(!matches_channel(&preview, Channel::Preview, "0.14.3"));
        let stable: url::Url = preview
            .as_str()
            .replace("0.14.2-preview.1", "0.14.2")
            .parse()
            .unwrap();
        assert!(matches_channel(&stable, Channel::Stable, "0.14.2"));
        assert!(!matches_channel(&stable, Channel::Preview, "0.14.2"));
    }
    #[test]
    fn signed_feed_authenticates_version_and_rejects_tampered_metadata() {
        let feed = include_bytes!("fixtures/feed.json");
        let signature = include_bytes!("fixtures/feed.json.sig");
        let public_key = include_str!("fixtures/test.pub").trim();
        assert_eq!(
            verify_feed(feed, signature, public_key).unwrap()["version"],
            "0.14.2"
        );
        assert_eq!(
            verify_feed(b"{\"version\":\"999.0.0\"}", signature, public_key).unwrap_err(),
            "app_update_signature_invalid"
        );
        assert!(verify_feed(feed, b"not a signature", public_key).is_err());
    }
    #[test]
    fn downloads_are_only_from_immutable_public_binary_release_paths() {
        for (raw, accepted) in [
            (
                "https://github.com/ZCchou/agent/releases/download/v1/setup.exe",
                true,
            ),
            (
                "http://github.com/ZCchou/agent/releases/download/v1/setup.exe",
                false,
            ),
            (
                "https://github.com/other/repo/releases/download/v1/setup.exe",
                false,
            ),
            (
                "https://github.com.example.test/ZCchou/agent/releases/download/v1/setup.exe",
                false,
            ),
            (
                "https://user:secret@github.com/ZCchou/agent/releases/download/v1/setup.exe",
                false,
            ),
        ] {
            assert_eq!(accepted_download_url(&raw.parse().unwrap()), accepted);
        }
    }
    #[test]
    fn old_id_and_concurrent_requests_cannot_claim_a_download() {
        let mut cache = CachedUpdate::default();
        cache.view.update_id = Some("new".into());
        assert_eq!(cache.check_identity("old").unwrap_err(), "app_update_stale");
        cache.view.state = "downloading".into();
        assert_eq!(cache.check_identity("new").unwrap_err(), "app_update_busy");
        cache.view.state = "ready".into();
        assert!(cache.check_identity("new").is_ok());
    }
}
