use std::sync::{Arc, Mutex};

use super::{
    error::SettingsError,
    model::{
        apply_vision_declaration, invocation_routes_for_profile, native_search_binding_for_profile,
        route_capabilities_for_model, AppSettings, InterfaceLanguage, ModelCapabilities,
        ModelCapabilityState, ModelSettings, ProviderModelSettings, ProviderProfileSettings,
        RemoteImagePolicy, RoutingPreferences, SearchBackendSettings, SidebarPreferences,
        WebSearchSettings, CURRENT_SETTINGS_SCHEMA_VERSION, DEFAULT_SIDEBAR_WIDTH_PX,
    },
    repository::SettingsRepository,
};

/// agent 全局设置管理器。
///
/// 所有持久设置的读写最终都应该经过这里。
///
/// Repository 只处理“怎么存”；
/// SettingsManager 负责：
///
/// - 默认值
/// - schema 检查
/// - 更新语义
/// - 并发读改写保护
pub struct SettingsManager {
    repository: Arc<dyn SettingsRepository>,

    /*
     * 设置修改本质上是：
     *
     * load -> modify -> save
     *
     * 这个锁确保多个并发 command 不会互相覆盖。
     */
    operation_lock: Mutex<()>,
}

impl SettingsManager {
    pub fn new(repository: Arc<dyn SettingsRepository>) -> Self {
        Self {
            repository,
            operation_lock: Mutex::new(()),
        }
    }

    /// 应用启动时调用。
    ///
    /// 第一次运行时会创建默认 settings.json；
    /// 已存在设置则验证 schema 后直接返回。
    #[cfg(test)]
    pub fn ensure_initialized(&self) -> Result<AppSettings, SettingsError> {
        self.initialize_for_startup(false)
    }

    pub(crate) fn initialize_for_startup(
        &self,
        migration_pending: bool,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;

        if migration_pending {
            return Ok(self.repository.load()?.unwrap_or_default());
        }

        let mut settings = self.load_or_create()?;
        settings.models.remove_demo_provider();

        // Re-save once at startup so the legacy split-key layout is
        // migrated to the current single root object.
        self.repository.save(&settings)?;

        Ok(settings)
    }

    pub(crate) fn wait_for_pending_operations(&self) -> Result<(), SettingsError> {
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| SettingsError::Storage("settings manager lock was poisoned".into()))?;
        Ok(())
    }

    pub fn get_settings(&self) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;

        self.load_or_create()
    }

    pub fn update_interface_language(
        &self,
        language: InterfaceLanguage,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;

        /*
         * 锁已经在本函数获取，因此这里不能再次调用
         * public get_settings()，否则会重复锁定同一个 Mutex。
         */
        let mut settings = self.load_or_create()?;

        settings.general.interface_language = language;

        self.repository.save(&settings)?;

        Ok(settings)
    }

    pub fn update_sidebar_preferences(
        &self,
        preferences: SidebarPreferences,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        settings.sidebar = preferences;
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn update_sidebar_width(&self, width_px: u16) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        if !(220..=480).contains(&width_px) {
            return Err(SettingsError::InvalidData(
                "sidebar width must be between 220 and 480 pixels".to_string(),
            ));
        }
        let mut settings = self.load_or_create()?;
        settings.sidebar.width_px = width_px;
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn update_remote_image_policy(
        &self,
        policy: RemoteImagePolicy,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        settings.content.remote_image_policy = policy;
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn update_routing_preferences(
        &self,
        routing: RoutingPreferences,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        settings.routing = routing;
        Self::validate_schema(&settings)?;
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn update_web_search_settings(
        &self,
        web_search: WebSearchSettings,
    ) -> Result<AppSettings, SettingsError> {
        if web_search.enabled && !web_search.privacy_acknowledged {
            return Err(SettingsError::InvalidData(
                "web search privacy notice must be acknowledged before enabling".to_string(),
            ));
        }
        for (label, value) in [
            ("search language", web_search.default_language.as_str()),
            ("search region", web_search.default_region.as_str()),
        ] {
            if value.is_empty()
                || value.len() > 32
                || !value
                    .chars()
                    .all(|character| character.is_ascii_alphanumeric() || character == '-')
            {
                return Err(SettingsError::InvalidData(format!("invalid {label}")));
            }
        }
        if let Some(default_id) = web_search.default_backend_id.as_deref() {
            if !web_search
                .backends
                .iter()
                .any(|backend| backend.id == default_id && backend.enabled)
            {
                return Err(SettingsError::InvalidData(
                    "default search backend must exist and be enabled".to_string(),
                ));
            }
        }
        for backend in &web_search.backends {
            validate_search_backend(backend)?;
        }
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        settings.web_search = web_search;
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn upsert_search_backend(
        &self,
        backend: SearchBackendSettings,
    ) -> Result<AppSettings, SettingsError> {
        validate_search_backend(&backend)?;
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        if let Some(existing) = settings
            .web_search
            .backends
            .iter_mut()
            .find(|item| item.id == backend.id)
        {
            *existing = backend;
        } else {
            settings.web_search.backends.push(backend);
        }
        let current_default_is_enabled = settings
            .web_search
            .default_backend_id
            .as_deref()
            .is_some_and(|default_id| {
                settings
                    .web_search
                    .backends
                    .iter()
                    .any(|item| item.id == default_id && item.enabled)
            });
        if !current_default_is_enabled {
            settings.web_search.default_backend_id = settings
                .web_search
                .backends
                .iter()
                .find(|item| item.enabled)
                .map(|item| item.id.clone());
        }
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn remove_search_backend(&self, backend_id: &str) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        settings
            .web_search
            .backends
            .retain(|item| item.id != backend_id);
        if settings.web_search.default_backend_id.as_deref() == Some(backend_id) {
            settings.web_search.default_backend_id = None;
        }
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn update_search_backend_status(
        &self,
        backend_id: &str,
        status: &str,
        tested_at: &str,
    ) -> Result<AppSettings, SettingsError> {
        if status.trim().is_empty()
            || status.len() > 96
            || !status.chars().all(|character| {
                character.is_ascii_alphanumeric() || matches!(character, '_' | '-')
            })
            || chrono::DateTime::parse_from_rfc3339(tested_at).is_err()
        {
            return Err(SettingsError::InvalidData(
                "invalid search backend health status".to_string(),
            ));
        }
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        let backend = settings
            .web_search
            .backends
            .iter_mut()
            .find(|backend| backend.id == backend_id)
            .ok_or_else(|| {
                SettingsError::InvalidData("search backend does not exist".to_string())
            })?;
        backend.last_status = Some(status.to_string());
        backend.last_tested_at = Some(tested_at.to_string());
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn update_model_route_capability_status(
        &self,
        provider_id: &str,
        model_id: &str,
        route_id: &str,
        capability_signature: &[String],
        status: &str,
        error_code: Option<&str>,
    ) -> Result<AppSettings, SettingsError> {
        if !matches!(status, "declared" | "verified" | "unsupported" | "unknown") {
            return Err(SettingsError::InvalidData(
                "invalid route capability status".to_string(),
            ));
        }
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        let provider = settings
            .models
            .providers
            .iter_mut()
            .find(|provider| provider.id == provider_id)
            .ok_or_else(|| SettingsError::InvalidData("provider does not exist".to_string()))?;
        let route = provider
            .invocation_routes
            .iter()
            .find(|route| {
                route.route_id == route_id
                    && capability_signature.iter().all(|capability| {
                        route.capabilities.iter().any(|value| value == capability)
                    })
            })
            .ok_or_else(|| {
                SettingsError::InvalidData("invocation route signature does not exist".to_string())
            })?;
        let is_native = capability_signature
            .iter()
            .any(|value| value == "native_web_search");
        let model = provider
            .models
            .iter_mut()
            .find(|model| model.id == model_id)
            .ok_or_else(|| {
                SettingsError::InvalidData("provider model does not exist".to_string())
            })?;
        let route_state = model
            .route_capabilities
            .iter_mut()
            .find(|state| {
                state.route_id == route_id
                    && state.capability_signature == capability_signature
                    && state.route_config_fingerprint == route.config_fingerprint
            })
            .ok_or_else(|| {
                SettingsError::InvalidData("model route capability does not exist".to_string())
            })?;
        let verified_at = if status == "verified" {
            Some(chrono::Utc::now().to_rfc3339())
        } else {
            None
        };
        route_state.status = status.to_string();
        route_state.error_code = error_code.map(str::to_string);
        route_state.verified_at.clone_from(&verified_at);
        if is_native {
            model.native_web_search_status = status.to_string();
            model.native_web_search_error_code = error_code.map(str::to_string);
            model.native_web_search_verified_at = verified_at;
        }
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn upsert_provider(
        &self,
        mut profile: ProviderProfileSettings,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        if profile.id.is_empty()
            || profile.name.trim().is_empty()
            || !matches!(
                profile.kind.as_str(),
                "fake" | "ollama" | "openai_compatible"
            )
        {
            return Err(SettingsError::InvalidData(
                "invalid provider profile".to_string(),
            ));
        }
        if !matches!(
            profile.endpoint_mode.as_str(),
            "preset" | "exact" | "legacy_auto_v1"
        ) || !matches!(
            profile.primary_protocol.as_str(),
            "memory" | "ollama" | "openai_chat" | "openai_responses" | "anthropic_messages"
        ) || !valid_native_search_binding(&profile.native_search)
            || profile.models.iter().any(|model| {
                model.id.trim().is_empty()
                    || !matches!(model.source.as_str(), "discovered" | "manual")
                    || !matches!(
                        model.status.as_str(),
                        "available" | "unavailable" | "unverified"
                    )
            })
        {
            return Err(SettingsError::InvalidData(
                "invalid provider model catalog".to_string(),
            ));
        }
        if profile.invocation_routes.is_empty() {
            profile.invocation_routes = invocation_routes_for_profile(
                &profile.preset_id,
                profile.effective_base_url(),
                if profile.preset_id == "custom" {
                    Some(profile.primary_protocol.as_str())
                } else {
                    None
                },
            );
        }
        for model in &mut profile.models {
            if model.route_capabilities.is_empty() {
                model.route_capabilities = route_capabilities_for_model(
                    &profile.invocation_routes,
                    &model.native_web_search_status,
                );
            }
        }
        let mut settings = self.load_or_create()?;
        if let Some(existing) = settings
            .models
            .providers
            .iter_mut()
            .find(|item| item.id == profile.id)
        {
            *existing = profile;
        } else {
            settings.models.providers.push(profile);
        }
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn remove_provider(&self, provider_id: &str) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        if provider_id == "builtin-demo" {
            return Err(SettingsError::InvalidData(
                "the built-in provider cannot be removed".to_string(),
            ));
        }
        let mut settings = self.load_or_create()?;
        if settings.models.default_provider_profile_id == provider_id {
            return Err(SettingsError::InvalidData(
                "choose another default model before deleting this provider".to_string(),
            ));
        }
        settings
            .models
            .providers
            .retain(|profile| profile.id != provider_id);
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub(crate) fn update_managed_default(
        &self,
        provider_id: &str,
        model_id: &str,
    ) -> Result<AppSettings, SettingsError> {
        if model_id.len() != 64
            || !model_id
                .bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
            || provider_id != format!("managed:{model_id}")
        {
            return Err(SettingsError::InvalidData(
                "Invalid managed model reference".into(),
            ));
        }
        let _guard = self
            .operation_lock
            .lock()
            .map_err(|_| SettingsError::Storage("settings manager lock was poisoned".into()))?;
        let mut settings = self.load_or_create()?;
        settings.models.default_provider_profile_id = provider_id.into();
        settings.models.default_model_id = model_id.into();
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn update_default_model(
        &self,
        provider_id: String,
        model_id: String,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        if model_id.trim().is_empty()
            || !settings.models.providers.iter().any(|item| {
                item.id == provider_id
                    && item.enabled
                    && item.models.iter().any(|model| {
                        model.id == model_id
                            && model.enabled
                            && model.status != "unavailable"
                            && model.capabilities.text != "no"
                    })
            })
        {
            return Err(SettingsError::InvalidData(
                "the default model selection is invalid".to_string(),
            ));
        }
        settings.models.default_provider_profile_id = provider_id.clone();
        settings.models.default_model_id = model_id.clone();
        if let Some(model) = settings
            .models
            .providers
            .iter_mut()
            .find(|profile| profile.id == provider_id)
            .and_then(|profile| profile.models.iter_mut().find(|model| model.id == model_id))
        {
            model.allow_automatic_routing = true;
        }
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn replace_provider_catalog(
        &self,
        provider_id: &str,
        discovered: Vec<ProviderModelSettings>,
        resolved_api_base_url: Option<String>,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        let profile = settings
            .models
            .providers
            .iter_mut()
            .find(|item| item.id == provider_id)
            .ok_or_else(|| {
                SettingsError::InvalidData("provider profile does not exist".to_string())
            })?;
        for existing in &mut profile.models {
            if existing.source == "discovered" {
                existing.status = "unavailable".to_string();
            }
        }
        for mut item in discovered {
            apply_vision_declaration(&profile.preset_id, &item.id, &mut item.capabilities);
            if let Some(existing) = profile.models.iter_mut().find(|model| model.id == item.id) {
                item.enabled = existing.enabled;
                item.allow_automatic_routing = existing.allow_automatic_routing;
                if existing.tool_mode != "unknown" {
                    item.tool_mode.clone_from(&existing.tool_mode);
                }
                if existing.capabilities.context_length_source == "manual" {
                    item.capabilities.context_length = existing.capabilities.context_length;
                    item.capabilities.max_output_tokens = existing.capabilities.max_output_tokens;
                    item.capabilities.context_length_source = "manual".to_string();
                }
                item.native_web_search_status
                    .clone_from(&existing.native_web_search_status);
                item.native_web_search_error_code
                    .clone_from(&existing.native_web_search_error_code);
                item.native_web_search_verified_at
                    .clone_from(&existing.native_web_search_verified_at);
                if matches!(
                    existing.capabilities.vision_source.as_str(),
                    "user_confirmed" | "runtime"
                ) {
                    item.capabilities
                        .vision
                        .clone_from(&existing.capabilities.vision);
                    item.capabilities
                        .vision_source
                        .clone_from(&existing.capabilities.vision_source);
                    item.capabilities
                        .vision_status
                        .clone_from(&existing.capabilities.vision_status);
                    item.capabilities
                        .vision_error_code
                        .clone_from(&existing.capabilities.vision_error_code);
                    item.capabilities
                        .vision_verified_at
                        .clone_from(&existing.capabilities.vision_verified_at);
                }
                *existing = item;
            } else {
                profile.models.push(item);
            }
        }
        profile.last_discovered_at = Some(chrono::Utc::now().to_rfc3339());
        if let Some(resolved) = resolved_api_base_url {
            profile.resolved_api_base_url = Some(resolved);
            profile.endpoint_mode = if profile.preset_id == "custom" {
                "exact"
            } else {
                "preset"
            }
            .to_string();
        }
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn set_provider_model_enabled(
        &self,
        provider_id: &str,
        model_id: &str,
        enabled: bool,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        if !enabled
            && settings.models.default_provider_profile_id == provider_id
            && settings.models.default_model_id == model_id
        {
            return Err(SettingsError::InvalidData(
                "choose another default model before disabling this model".to_string(),
            ));
        }
        let profile = settings
            .models
            .providers
            .iter_mut()
            .find(|item| item.id == provider_id)
            .ok_or_else(|| {
                SettingsError::InvalidData("provider profile does not exist".to_string())
            })?;
        let model = profile
            .models
            .iter_mut()
            .find(|item| item.id == model_id)
            .ok_or_else(|| {
                SettingsError::InvalidData("provider model does not exist".to_string())
            })?;
        model.enabled = enabled;
        if !enabled {
            model.allow_automatic_routing = false;
        }
        if enabled {
            profile.enabled = true;
        }
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn set_model_automatic_routing(
        &self,
        provider_id: &str,
        model_id: &str,
        allowed: bool,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        let is_default = settings.models.default_provider_profile_id == provider_id
            && settings.models.default_model_id == model_id;
        if is_default && !allowed {
            return Err(SettingsError::InvalidData(
                "choose another default model before disabling automatic routing".to_string(),
            ));
        }
        let profile = settings
            .models
            .providers
            .iter_mut()
            .find(|profile| profile.id == provider_id)
            .ok_or_else(|| {
                SettingsError::InvalidData("provider profile does not exist".to_string())
            })?;
        if profile.kind == "fake" && allowed {
            return Err(SettingsError::InvalidData(
                "the built-in demo cannot participate in production automatic routing".to_string(),
            ));
        }
        let model = profile
            .models
            .iter_mut()
            .find(|model| model.id == model_id)
            .ok_or_else(|| {
                SettingsError::InvalidData("provider model does not exist".to_string())
            })?;
        if allowed && (!model.enabled || model.status == "unavailable") {
            return Err(SettingsError::InvalidData(
                "enable the model before allowing automatic routing".to_string(),
            ));
        }
        model.allow_automatic_routing = allowed;
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn update_model_tool_mode(
        &self,
        provider_id: &str,
        model_id: &str,
        tool_mode: &str,
    ) -> Result<AppSettings, SettingsError> {
        if !matches!(tool_mode, "native" | "structured" | "text_only" | "unknown") {
            return Err(SettingsError::InvalidData(
                "invalid model tool mode".to_string(),
            ));
        }
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        let model = settings
            .models
            .providers
            .iter_mut()
            .find(|item| item.id == provider_id)
            .and_then(|provider| provider.models.iter_mut().find(|item| item.id == model_id))
            .ok_or_else(|| {
                SettingsError::InvalidData("provider model does not exist".to_string())
            })?;
        model.tool_mode = tool_mode.to_string();
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn update_model_context_limits(
        &self,
        provider_id: &str,
        model_id: &str,
        context_length: Option<u64>,
        max_output_tokens: Option<u64>,
    ) -> Result<AppSettings, SettingsError> {
        if context_length.is_some_and(|value| !(512..=2_000_000).contains(&value)) {
            return Err(SettingsError::InvalidData(
                "model context length must be between 512 and 2000000".to_string(),
            ));
        }
        if max_output_tokens.is_some_and(|value| value == 0 || value > 200_000) {
            return Err(SettingsError::InvalidData(
                "model output limit must be between 1 and 200000".to_string(),
            ));
        }
        if let (Some(window), Some(output)) = (context_length, max_output_tokens) {
            if output >= window {
                return Err(SettingsError::InvalidData(
                    "model output limit must be smaller than its context length".to_string(),
                ));
            }
        }
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        let model = settings
            .models
            .providers
            .iter_mut()
            .find(|item| item.id == provider_id)
            .and_then(|provider| provider.models.iter_mut().find(|item| item.id == model_id))
            .ok_or_else(|| {
                SettingsError::InvalidData("provider model does not exist".to_string())
            })?;
        model.capabilities.context_length = context_length;
        model.capabilities.max_output_tokens = max_output_tokens;
        model.capabilities.context_length_source = if context_length.is_some() {
            "manual".to_string()
        } else {
            "fallback".to_string()
        };
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn update_model_vision_confirmation(
        &self,
        provider_id: &str,
        model_id: &str,
        confirmed: Option<bool>,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        let profile = settings
            .models
            .providers
            .iter_mut()
            .find(|profile| profile.id == provider_id)
            .ok_or_else(|| {
                SettingsError::InvalidData("provider profile does not exist".to_string())
            })?;
        let preset_id = profile.preset_id.clone();
        let model = profile
            .models
            .iter_mut()
            .find(|model| model.id == model_id)
            .ok_or_else(|| {
                SettingsError::InvalidData("provider model does not exist".to_string())
            })?;
        match confirmed {
            Some(true) => {
                model.capabilities.vision = "yes".to_string();
                model.capabilities.vision_source = "user_confirmed".to_string();
                model.capabilities.vision_status = "declared".to_string();
                model.capabilities.vision_error_code = None;
                model.capabilities.vision_verified_at = None;
            }
            Some(false) => {
                model.capabilities.vision = "no".to_string();
                model.capabilities.vision_source = "user_confirmed".to_string();
                model.capabilities.vision_status = "unsupported".to_string();
                model.capabilities.vision_error_code = None;
                model.capabilities.vision_verified_at = None;
            }
            None => {
                model.capabilities.vision = "unknown".to_string();
                model.capabilities.vision_source = "unknown".to_string();
                model.capabilities.vision_status = "unknown".to_string();
                model.capabilities.vision_error_code = None;
                model.capabilities.vision_verified_at = None;
                apply_vision_declaration(&preset_id, model_id, &mut model.capabilities);
            }
        }
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn update_model_routing_metadata(
        &self,
        provider_id: &str,
        model_id: &str,
        input_price: Option<String>,
        output_price: Option<String>,
        quality_score: Option<u8>,
        priority: i8,
    ) -> Result<AppSettings, SettingsError> {
        fn validate_price(value: &Option<String>) -> Result<(), SettingsError> {
            if let Some(raw) = value {
                let parsed = raw.parse::<f64>().map_err(|_| {
                    SettingsError::InvalidData("model price must be a decimal number".to_string())
                })?;
                if !parsed.is_finite() || !(0.0..=100_000.0).contains(&parsed) {
                    return Err(SettingsError::InvalidData(
                        "model price must be between 0 and 100000".to_string(),
                    ));
                }
            }
            Ok(())
        }
        validate_price(&input_price)?;
        validate_price(&output_price)?;
        if quality_score.is_some_and(|value| value > 100) || !(-10..=10).contains(&priority) {
            return Err(SettingsError::InvalidData(
                "model routing score or priority is outside its safe range".to_string(),
            ));
        }
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        let model = settings
            .models
            .providers
            .iter_mut()
            .find(|item| item.id == provider_id)
            .and_then(|provider| provider.models.iter_mut().find(|item| item.id == model_id))
            .ok_or_else(|| {
                SettingsError::InvalidData("provider model does not exist".to_string())
            })?;
        model.capabilities.input_price_per_million = input_price;
        model.capabilities.output_price_per_million = output_price;
        model.capabilities.routing_quality_score = quality_score;
        model.capabilities.routing_priority = priority;
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn update_model_vision_runtime_status(
        &self,
        provider_id: &str,
        model_id: &str,
        status: &str,
        error_code: Option<&str>,
    ) -> Result<AppSettings, SettingsError> {
        if !matches!(status, "verified" | "unsupported") {
            return Err(SettingsError::InvalidData(
                "invalid image capability status".to_string(),
            ));
        }
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        let model = settings
            .models
            .providers
            .iter_mut()
            .find(|profile| profile.id == provider_id)
            .and_then(|profile| profile.models.iter_mut().find(|model| model.id == model_id))
            .ok_or_else(|| {
                SettingsError::InvalidData("provider model does not exist".to_string())
            })?;
        if status == "verified" {
            model.capabilities.vision = "yes".to_string();
            model.capabilities.vision_source = "runtime".to_string();
            model.capabilities.vision_verified_at = Some(chrono::Utc::now().to_rfc3339());
        } else if status == "unsupported" {
            model.capabilities.vision = "no".to_string();
            model.capabilities.vision_source = "runtime".to_string();
            model.capabilities.vision_verified_at = None;
        }
        model.capabilities.vision_status = status.to_string();
        model.capabilities.vision_error_code = error_code.map(str::to_string);
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn add_manual_provider_model(
        &self,
        provider_id: &str,
        model_id: String,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let normalized = model_id.trim();
        if normalized.is_empty() || normalized.len() > 200 {
            return Err(SettingsError::InvalidData("invalid model id".to_string()));
        }
        let mut settings = self.load_or_create()?;
        let profile = settings
            .models
            .providers
            .iter_mut()
            .find(|item| item.id == provider_id)
            .ok_or_else(|| {
                SettingsError::InvalidData("provider profile does not exist".to_string())
            })?;
        if !profile.models.iter().any(|item| item.id == normalized) {
            let mut capabilities = ModelCapabilities::default();
            apply_vision_declaration(&profile.preset_id, normalized, &mut capabilities);
            profile.models.push(ProviderModelSettings {
                id: normalized.to_string(),
                source: "manual".to_string(),
                status: "unverified".to_string(),
                enabled: false,
                allow_automatic_routing: false,
                tool_mode: "unknown".to_string(),
                capabilities,
                capability_states: Vec::new(),
                last_seen_at: None,
                native_web_search_status: if profile.native_search.adapter_id == "none" {
                    "not_applicable".to_string()
                } else {
                    "declared".to_string()
                },
                native_web_search_error_code: None,
                native_web_search_verified_at: None,
                route_capabilities: route_capabilities_for_model(
                    &profile.invocation_routes,
                    if profile.native_search.adapter_id == "none" {
                        "not_applicable"
                    } else {
                        "declared"
                    },
                ),
                tool_contracts: Vec::new(),
            });
        }
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn remove_manual_provider_model(
        &self,
        provider_id: &str,
        model_id: &str,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        if settings.models.default_provider_profile_id == provider_id
            && settings.models.default_model_id == model_id
        {
            return Err(SettingsError::InvalidData(
                "choose another default model before removing this model".to_string(),
            ));
        }
        let profile = settings
            .models
            .providers
            .iter_mut()
            .find(|item| item.id == provider_id)
            .ok_or_else(|| {
                SettingsError::InvalidData("provider profile does not exist".to_string())
            })?;
        let before = profile.models.len();
        profile
            .models
            .retain(|model| !(model.id == model_id && model.source == "manual"));
        if profile.models.len() == before {
            return Err(SettingsError::InvalidData(
                "manual provider model does not exist".to_string(),
            ));
        }
        self.repository.save(&settings)?;
        Ok(settings)
    }

    pub fn set_provider_enabled(
        &self,
        provider_id: &str,
        enabled: bool,
    ) -> Result<AppSettings, SettingsError> {
        let _guard = self.operation_lock.lock().map_err(|_| {
            SettingsError::Storage("settings manager lock was poisoned".to_string())
        })?;
        let mut settings = self.load_or_create()?;
        if !enabled && settings.models.default_provider_profile_id == provider_id {
            return Err(SettingsError::InvalidData(
                "choose another default model before disabling this provider".to_string(),
            ));
        }
        let profile = settings
            .models
            .providers
            .iter_mut()
            .find(|item| item.id == provider_id)
            .ok_or_else(|| {
                SettingsError::InvalidData("provider profile does not exist".to_string())
            })?;
        profile.enabled = enabled;
        self.repository.save(&settings)?;
        Ok(settings)
    }

    fn load_or_create(&self) -> Result<AppSettings, SettingsError> {
        if crate::profile_migration::blocks_core() {
            return Ok(self.repository.load()?.unwrap_or_default());
        }
        match self.repository.load()? {
            Some(mut settings) => {
                if settings.schema_version == 1 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    settings.sidebar = SidebarPreferences::default();
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 2 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    settings.sidebar.width_px = DEFAULT_SIDEBAR_WIDTH_PX;
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 3 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    settings.models = ModelSettings::default();
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 4 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    for profile in &mut settings.models.providers {
                        if profile.kind == "fake" {
                            profile.preset_id = "builtin_demo".to_string();
                            profile.endpoint_mode = "preset".to_string();
                            profile.resolved_api_base_url = Some(profile.entered_base_url.clone());
                            if profile.models.is_empty() {
                                profile.models.push(ProviderModelSettings {
                                    id: "demo".to_string(),
                                    source: "discovered".to_string(),
                                    status: "available".to_string(),
                                    enabled: true,
                                    allow_automatic_routing: false,
                                    tool_mode: "native".to_string(),
                                    capabilities: ModelCapabilities {
                                        text: "yes".to_string(),
                                        ..Default::default()
                                    },
                                    capability_states: Vec::new(),
                                    last_seen_at: None,
                                    native_web_search_status: "not_applicable".to_string(),
                                    native_web_search_error_code: None,
                                    native_web_search_verified_at: None,
                                    route_capabilities: Vec::new(),
                                    tool_contracts: Vec::new(),
                                });
                            }
                        } else if profile.kind == "ollama" {
                            profile.preset_id = "ollama".to_string();
                            profile.endpoint_mode = "preset".to_string();
                            profile.resolved_api_base_url = Some(profile.entered_base_url.clone());
                        } else {
                            profile.endpoint_mode = "legacy_auto_v1".to_string();
                        }
                    }
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 5 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 6 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    for profile in &mut settings.models.providers {
                        for model in &mut profile.models {
                            model.capabilities.context_length_source =
                                if model.capabilities.context_length.is_some() {
                                    "discovered".to_string()
                                } else {
                                    "fallback".to_string()
                                };
                        }
                    }
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 7 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    settings.web_search = WebSearchSettings::default();
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 8 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    Self::repair_native_search_bindings(&mut settings);
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 9 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    for profile in &mut settings.models.providers {
                        for model in &mut profile.models {
                            apply_vision_declaration(
                                &profile.preset_id,
                                &model.id,
                                &mut model.capabilities,
                            );
                        }
                    }
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 10 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    settings.routing = crate::settings::model::RoutingPreferences::default();
                    self.repository.save(&settings)?;
                } else if matches!(settings.schema_version, 11 | 12) {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    Self::repair_invocation_routes(&mut settings);
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 13 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    Self::repair_automatic_routing_authorization(&mut settings);
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 14 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    Self::repair_invocation_routes(&mut settings);
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 15 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    Self::migrate_route_health_v1(&mut settings);
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 16 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    for profile in &mut settings.models.providers {
                        for model in &mut profile.models {
                            if model.tool_contracts.is_empty() && model.tool_mode != "text_only" {
                                model.tool_contracts = Vec::new();
                            }
                        }
                    }
                    self.repository.save(&settings)?;
                } else if settings.schema_version == 17 {
                    // Schema 18 introduces capability-scoped state. Legacy
                    // verified search is intentionally downgraded to declared
                    // until the route-level verifier runs again; credentials,
                    // URLs and model enablement are left untouched.
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    Self::migrate_capability_contract(&mut settings);
                    self.repository.save(&settings)?;
                }
                if settings.schema_version == 18 {
                    settings.schema_version = CURRENT_SETTINGS_SCHEMA_VERSION;
                    self.repository.save(&settings)?;
                }
                if Self::repair_automatic_routing_authorization(&mut settings) {
                    self.repository.save(&settings)?;
                }
                if Self::repair_invocation_routes(&mut settings) {
                    self.repository.save(&settings)?;
                }
                if Self::repair_multimodal_capabilities(&mut settings) {
                    self.repository.save(&settings)?;
                }
                if Self::migrate_route_health_v1(&mut settings) {
                    self.repository.save(&settings)?;
                }
                Self::validate_schema(&settings)?;

                Ok(settings)
            }

            None => {
                let settings = AppSettings::default();

                self.repository.save(&settings)?;

                Ok(settings)
            }
        }
    }

    fn repair_automatic_routing_authorization(settings: &mut AppSettings) -> bool {
        let default_provider_id = settings.models.default_provider_profile_id.clone();
        let default_model_id = settings.models.default_model_id.clone();
        let mut changed = false;
        for profile in &mut settings.models.providers {
            for model in &mut profile.models {
                if (!model.enabled || profile.kind == "fake") && model.allow_automatic_routing {
                    model.allow_automatic_routing = false;
                    changed = true;
                }
                let is_default = profile.kind != "fake"
                    && profile.id == default_provider_id
                    && model.id == default_model_id
                    && model.enabled
                    && model.status != "unavailable";
                if is_default && !model.allow_automatic_routing {
                    model.allow_automatic_routing = true;
                    changed = true;
                }
            }
        }
        changed
    }

    fn migrate_capability_contract(settings: &mut AppSettings) -> bool {
        let mut changed = false;
        for profile in &mut settings.models.providers {
            for model in &mut profile.models {
                if model.capability_states.is_empty() {
                    let declared = |value: &str| match value {
                        "yes" | "declared" | "verified" => "declared".to_string(),
                        "no" | "unsupported" => "unsupported".to_string(),
                        _ => "unknown".to_string(),
                    };
                    model.capability_states = [
                        ("text_generation", declared(&model.capabilities.text)),
                        ("vision_input", declared(&model.capabilities.vision_status)),
                        ("reasoning", declared(&model.capabilities.reasoning)),
                        ("function_calling", declared(&model.capabilities.tools)),
                        ("web_search", declared(&model.native_web_search_status)),
                        ("structured_web_evidence", "unknown".to_string()),
                        ("file_read", "unknown".to_string()),
                        ("file_write", "unknown".to_string()),
                    ]
                    .into_iter()
                    .map(|(capability_id, status)| ModelCapabilityState {
                        capability_id: capability_id.to_string(),
                        declared_status: status,
                        verified_status: "unknown".to_string(),
                        limits: Default::default(),
                        last_verified_at: None,
                        error_code: None,
                    })
                    .collect();
                    changed = true;
                }
                for state in &mut model.route_capabilities {
                    if state.capability_id.is_empty() {
                        state.capability_id = if state
                            .capability_signature
                            .iter()
                            .any(|value| value == "structured_web_evidence")
                        {
                            "structured_web_evidence"
                        } else if state
                            .capability_signature
                            .iter()
                            .any(|value| value == "native_web_search" || value == "web_search")
                        {
                            "web_search"
                        } else {
                            "text_generation"
                        }
                        .to_string();
                        state.evidence_kind = if state.capability_id == "web_search"
                            && state.route_id.contains("deepseek")
                        {
                            "structured".to_string()
                        } else if state.capability_id == "web_search" {
                            "opaque".to_string()
                        } else {
                            "none".to_string()
                        };
                        changed = true;
                    }
                }
            }
        }
        changed
    }

    fn repair_native_search_bindings(settings: &mut AppSettings) -> bool {
        let mut changed = false;
        for profile in &mut settings.models.providers {
            let (mapped_primary_protocol, expected) =
                native_search_binding_for_profile(&profile.preset_id, profile.effective_base_url());
            let primary_protocol = if profile.preset_id == "custom"
                && matches!(
                    profile.primary_protocol.as_str(),
                    "openai_chat" | "openai_responses" | "anthropic_messages"
                ) {
                profile.primary_protocol.clone()
            } else {
                mapped_primary_protocol
            };
            if profile.primary_protocol != primary_protocol || profile.native_search != expected {
                profile.primary_protocol = primary_protocol;
                profile.native_search = expected;
                let declared = profile.native_search.adapter_id != "none";
                for model in &mut profile.models {
                    model.native_web_search_status = if declared {
                        "declared".to_string()
                    } else {
                        "not_applicable".to_string()
                    };
                    model.native_web_search_error_code = None;
                    model.native_web_search_verified_at = None;
                }
                changed = true;
            }
        }
        changed
    }

    fn repair_invocation_routes(settings: &mut AppSettings) -> bool {
        let mut changed = false;
        for profile in &mut settings.models.providers {
            let expected = invocation_routes_for_profile(
                &profile.preset_id,
                profile.effective_base_url(),
                if profile.preset_id == "custom" {
                    Some(profile.primary_protocol.as_str())
                } else {
                    None
                },
            );
            let official = expected
                .iter()
                .all(|route| route.source == "official_preset");
            if profile.invocation_routes.is_empty()
                || (official && profile.invocation_routes != expected)
            {
                let old_routes = profile.invocation_routes.clone();
                profile.invocation_routes = expected;
                for model in &mut profile.models {
                    let old_states = model.route_capabilities.clone();
                    let mut states = route_capabilities_for_model(
                        &profile.invocation_routes,
                        &model.native_web_search_status,
                    );
                    for state in &mut states {
                        if let Some(previous) = old_states.iter().find(|previous| {
                            previous.route_id == state.route_id
                                && previous.capability_signature == state.capability_signature
                                && previous.route_config_fingerprint
                                    == state.route_config_fingerprint
                        }) {
                            *state = previous.clone();
                        }
                        if official
                            && state.route_id == "deepseek.anthropic_web"
                            && state.status == "unsupported"
                        {
                            state.status = "declared".to_string();
                            state.error_code = None;
                            state.verified_at = None;
                        }
                    }
                    model.route_capabilities = states;
                }
                changed |= old_routes != profile.invocation_routes;
            } else {
                for model in &mut profile.models {
                    let expected_states = route_capabilities_for_model(
                        &profile.invocation_routes,
                        &model.native_web_search_status,
                    );
                    for expected_state in expected_states {
                        let exists = model.route_capabilities.iter().any(|state| {
                            state.route_id == expected_state.route_id
                                && state.capability_signature == expected_state.capability_signature
                                && state.route_config_fingerprint
                                    == expected_state.route_config_fingerprint
                        });
                        if !exists {
                            model.route_capabilities.push(expected_state);
                            changed = true;
                        }
                    }
                }
            }
            if official {
                for model in &mut profile.models {
                    for state in &mut model.route_capabilities {
                        if state.route_id == "deepseek.anthropic_web"
                            && state
                                .capability_signature
                                .iter()
                                .any(|value| value == "native_web_search")
                            && state.status == "unsupported"
                        {
                            state.status = "declared".to_string();
                            state.error_code = None;
                            state.verified_at = None;
                            changed = true;
                        }
                    }
                }
            }

            if let Some(primary) = profile
                .invocation_routes
                .iter()
                .find(|route| route.route_role == "primary")
            {
                profile.primary_protocol.clone_from(&primary.protocol);
            }
            if let Some(native) = profile.invocation_routes.iter().find(|route| {
                route
                    .capabilities
                    .iter()
                    .any(|value| value == "native_web_search")
            }) {
                profile.native_search.adapter_id = match native.adapter_id.as_str() {
                    "openai.responses" => "openai_responses_web_search",
                    "kimi.openai_chat" | "kimi.web.builtin" => "kimi_builtin_web_search",
                    "kimi.web.formula" => "kimi_formula_web_search",
                    "deepseek.anthropic_web" => "anthropic_messages_web_search",
                    other => other,
                }
                .to_string();
                profile.native_search.protocol.clone_from(&native.protocol);
                profile
                    .native_search
                    .endpoint_base_url
                    .clone_from(&native.endpoint_base_url);
                profile.native_search.source.clone_from(&native.source);
                profile
                    .native_search
                    .adapter_version
                    .clone_from(&native.manifest_version);
                for model in &mut profile.models {
                    if let Some(state) = model.route_capabilities.iter().find(|state| {
                        state.route_id == native.route_id
                            && state
                                .capability_signature
                                .iter()
                                .any(|value| value == "native_web_search")
                    }) {
                        model.native_web_search_status.clone_from(&state.status);
                        model
                            .native_web_search_error_code
                            .clone_from(&state.error_code);
                        model
                            .native_web_search_verified_at
                            .clone_from(&state.verified_at);
                    }
                }
            } else {
                profile.native_search = Default::default();
                for model in &mut profile.models {
                    model.native_web_search_status = "not_applicable".to_string();
                    model.native_web_search_error_code = None;
                    model.native_web_search_verified_at = None;
                }
            }
        }
        changed
    }

    fn repair_multimodal_capabilities(settings: &mut AppSettings) -> bool {
        let mut changed = false;
        for profile in &mut settings.models.providers {
            for model in &mut profile.models {
                if model.capabilities.vision_source == "official_manifest" {
                    let before = model.capabilities.clone();
                    model.capabilities.vision = "unknown".to_string();
                    model.capabilities.vision_source = "unknown".to_string();
                    model.capabilities.vision_status = "unknown".to_string();
                    model.capabilities.vision_error_code = None;
                    model.capabilities.vision_verified_at = None;
                    apply_vision_declaration(
                        &profile.preset_id,
                        &model.id,
                        &mut model.capabilities,
                    );
                    changed |= model.capabilities != before;
                }
                if model.native_web_search_status == "temporary_error"
                    && matches!(
                        model.native_web_search_error_code.as_deref(),
                        Some(
                            "image_format_unsupported"
                                | "image_payload_too_large"
                                | "multimodal_route_unsupported"
                                | "multimodal_protocol_unsupported"
                                | "model_vision_unsupported"
                        )
                    )
                {
                    model.native_web_search_status = if profile.native_search.adapter_id == "none" {
                        "not_applicable".to_string()
                    } else {
                        "declared".to_string()
                    };
                    model.native_web_search_error_code = None;
                    model.native_web_search_verified_at = None;
                    changed = true;
                }
            }
        }
        changed
    }

    fn migrate_route_health_v1(settings: &mut AppSettings) -> bool {
        let mut changed = false;
        for profile in &mut settings.models.providers {
            for model in &mut profile.models {
                if model.capabilities.vision_status == "temporary_error" {
                    if profile.preset_id == "custom" {
                        model.capabilities.vision = "unknown".to_string();
                        model.capabilities.vision_source = "unknown".to_string();
                        model.capabilities.vision_status = "unknown".to_string();
                    } else {
                        model.capabilities.vision = "unknown".to_string();
                        model.capabilities.vision_source = "unknown".to_string();
                        model.capabilities.vision_status = "unknown".to_string();
                        apply_vision_declaration(
                            &profile.preset_id,
                            &model.id,
                            &mut model.capabilities,
                        );
                    }
                    model.capabilities.vision_error_code = None;
                    model.capabilities.vision_verified_at = None;
                    changed = true;
                }
                for state in &mut model.route_capabilities {
                    if state.status != "temporary_error" {
                        continue;
                    }
                    let official = profile.invocation_routes.iter().any(|route| {
                        route.route_id == state.route_id
                            && route.config_fingerprint == state.route_config_fingerprint
                            && route.source == "official_preset"
                    });
                    state.status = if official { "declared" } else { "unknown" }.to_string();
                    state.error_code = None;
                    state.verified_at = None;
                    changed = true;
                }
                if model.native_web_search_status == "temporary_error" {
                    model.native_web_search_status = if profile.preset_id == "custom" {
                        "unknown".to_string()
                    } else if profile.native_search.adapter_id == "none" {
                        "not_applicable".to_string()
                    } else {
                        "declared".to_string()
                    };
                    model.native_web_search_error_code = None;
                    model.native_web_search_verified_at = None;
                    changed = true;
                }
            }
        }
        changed
    }

    fn validate_schema(settings: &AppSettings) -> Result<(), SettingsError> {
        if settings.schema_version != CURRENT_SETTINGS_SCHEMA_VERSION {
            return Err(SettingsError::UnsupportedSchemaVersion(
                settings.schema_version,
            ));
        }
        if settings.routing.new_conversation_mode != "automatic"
            || !matches!(
                settings.routing.default_preset.as_str(),
                "quality" | "balanced" | "economy" | "local_first"
            )
            || !(1..=6).contains(&settings.routing.max_model_calls_per_message)
            || settings.routing.max_specialist_steps > 2
            || !(30..=180).contains(&settings.routing.intent_timeout_seconds)
            || [
                settings.routing.main_model_idle_seconds,
                settings.routing.auxiliary_model_idle_seconds,
            ]
            .iter()
            .any(|value| *value != 0 && !(60..=86400).contains(value))
            || settings
                .routing
                .max_extra_online_cost_usd
                .parse::<f64>()
                .map_or(true, |value| !(0.0..=5.0).contains(&value))
        {
            return Err(SettingsError::InvalidData(
                "invalid routing preferences".to_string(),
            ));
        }
        for profile in &settings.models.providers {
            for model in &profile.models {
                if model.allow_automatic_routing
                    && (!model.enabled || model.status == "unavailable" || profile.kind == "fake")
                {
                    return Err(SettingsError::InvalidData(
                        "invalid automatic model authorization".to_string(),
                    ));
                }
            }
        }

        Ok(())
    }
}

fn valid_native_search_binding(binding: &crate::settings::model::NativeSearchBinding) -> bool {
    let protocol_matches = matches!(
        (binding.adapter_id.as_str(), binding.protocol.as_str()),
        ("none", "none")
            | ("openai_responses_web_search", "openai_responses")
            | ("kimi_builtin_web_search", "openai_chat")
            | ("anthropic_messages_web_search", "anthropic_messages")
    );
    protocol_matches
        && matches!(binding.source.as_str(), "official_preset" | "manual")
        && !binding.adapter_version.trim().is_empty()
}

fn validate_search_backend(backend: &SearchBackendSettings) -> Result<(), SettingsError> {
    if backend.id.trim().is_empty()
        || backend.name.trim().is_empty()
        || !matches!(backend.kind.as_str(), "tavily" | "searxng")
    {
        return Err(SettingsError::InvalidData(
            "invalid search backend configuration".to_string(),
        ));
    }
    let parsed = url::Url::parse(&backend.base_url)
        .map_err(|_| SettingsError::InvalidData("search backend URL is invalid".to_string()))?;
    let host = parsed.host_str().unwrap_or_default();
    let loopback = matches!(host, "localhost" | "127.0.0.1" | "::1");
    if parsed.scheme() != "https" && !(loopback && parsed.scheme() == "http") {
        return Err(SettingsError::InvalidData(
            "remote search backends must use HTTPS".to_string(),
        ));
    }
    if !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        return Err(SettingsError::InvalidData(
            "search backend URL cannot contain credentials, query, or fragment".to_string(),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use super::*;

    use crate::settings::{
        model::{GeneralSettings, NativeSearchBinding},
        repository::SettingsRepository,
    };

    #[derive(Default)]
    struct MemorySettingsRepository {
        settings: Mutex<Option<AppSettings>>,
    }

    impl MemorySettingsRepository {
        fn with_settings(settings: AppSettings) -> Self {
            Self {
                settings: Mutex::new(Some(settings)),
            }
        }

        fn snapshot(&self) -> Option<AppSettings> {
            self.settings.lock().unwrap().clone()
        }
    }

    impl SettingsRepository for MemorySettingsRepository {
        fn load(&self) -> Result<Option<AppSettings>, SettingsError> {
            Ok(self.settings.lock().unwrap().clone())
        }

        fn save(&self, settings: &AppSettings) -> Result<(), SettingsError> {
            *self.settings.lock().unwrap() = Some(settings.clone());

            Ok(())
        }
    }

    struct FailingSettingsRepository {
        settings: Mutex<Option<AppSettings>>,
    }

    impl SettingsRepository for FailingSettingsRepository {
        fn load(&self) -> Result<Option<AppSettings>, SettingsError> {
            Ok(self.settings.lock().unwrap().clone())
        }

        fn save(&self, _settings: &AppSettings) -> Result<(), SettingsError> {
            Err(SettingsError::Storage("simulated save failure".to_string()))
        }
    }

    #[test]
    fn intent_timeout_and_residency_defaults_migrate_and_validate() {
        let mut original = AppSettings::default();
        original.schema_version = 18;
        let manager =
            SettingsManager::new(Arc::new(MemorySettingsRepository::with_settings(original)));
        let loaded = manager.get_settings().unwrap();
        assert_eq!(loaded.schema_version, 19);
        let value = serde_json::to_value(&loaded.routing).unwrap();
        assert_eq!(value["intent_timeout_seconds"], 90);
        assert_eq!(value["main_model_idle_seconds"], 1800);
        assert_eq!(value["auxiliary_model_idle_seconds"], 1800);
        for (field, invalid) in [
            ("intent_timeout_seconds", 29),
            ("intent_timeout_seconds", 181),
            ("main_model_idle_seconds", 59),
            ("auxiliary_model_idle_seconds", 86401),
        ] {
            let mut changed = value.clone();
            changed[field] = serde_json::json!(invalid);
            let preferences = serde_json::from_value(changed).unwrap();
            assert!(manager.update_routing_preferences(preferences).is_err());
        }
        let mut manual = value;
        manual["main_model_idle_seconds"] = serde_json::json!(0);
        manual["auxiliary_model_idle_seconds"] = serde_json::json!(0);
        assert!(manager
            .update_routing_preferences(serde_json::from_value(manual).unwrap())
            .is_ok());
    }

    #[test]
    fn failed_update_does_not_change_repository_state() {
        let original = AppSettings::default();

        let repository = Arc::new(FailingSettingsRepository {
            settings: Mutex::new(Some(original.clone())),
        });

        let manager = SettingsManager::new(repository.clone());

        assert!(manager
            .update_interface_language(InterfaceLanguage::English,)
            .is_err(),);

        assert_eq!(repository.settings.lock().unwrap().clone(), Some(original),);
    }

    #[test]
    fn failed_remote_image_update_does_not_change_repository_state() {
        let original = AppSettings::default();
        let repository = Arc::new(FailingSettingsRepository {
            settings: Mutex::new(Some(original.clone())),
        });
        let manager = SettingsManager::new(repository.clone());

        assert!(manager
            .update_remote_image_policy(RemoteImagePolicy::Allow)
            .is_err());
        assert_eq!(repository.settings.lock().unwrap().clone(), Some(original));
    }

    #[test]
    fn initializes_default_settings_when_repository_is_empty() {
        let repository = Arc::new(MemorySettingsRepository::default());

        let manager = SettingsManager::new(repository.clone());

        let settings = manager.ensure_initialized().unwrap();

        assert!(settings.models.providers.is_empty());
        assert!(settings.models.default_provider_profile_id.is_empty());
        assert!(settings.models.default_model_id.is_empty());
        assert_eq!(repository.snapshot(), Some(settings));
    }

    #[test]
    fn pending_migration_initialization_reads_without_normalizing_or_saving() {
        let original = AppSettings::default();
        let repository = Arc::new(FailingSettingsRepository {
            settings: Mutex::new(Some(original.clone())),
        });
        let manager = SettingsManager::new(repository);
        assert_eq!(manager.initialize_for_startup(true).unwrap(), original);
    }

    #[test]
    fn startup_removes_legacy_demo_and_preserves_real_model_selection() {
        let mut settings = AppSettings::default();
        let mut local = settings.models.providers[0].clone();
        local.id = "local-service".into();
        local.kind = "openai_compatible".into();
        local.preset_id = "custom".into();
        local.entered_base_url = "http://127.0.0.1:8080/v1".into();
        local.resolved_api_base_url = None;
        settings.models.providers.push(local);
        settings.models.default_provider_profile_id = "local-service".into();
        settings.models.default_model_id = "qwen".into();
        let repository = Arc::new(MemorySettingsRepository::with_settings(settings));
        let manager = SettingsManager::new(repository.clone());
        let migrated = manager.ensure_initialized().unwrap();
        assert_eq!(migrated.models.providers.len(), 1);
        assert_eq!(migrated.models.providers[0].id, "local-service");
        assert_eq!(migrated.models.default_provider_profile_id, "local-service");
        assert_eq!(migrated.models.default_model_id, "qwen");
        assert_eq!(repository.snapshot(), Some(migrated.clone()));
        assert_eq!(manager.ensure_initialized().unwrap(), migrated);
    }

    #[test]
    fn loads_existing_valid_settings_without_resetting_them() {
        let expected = AppSettings {
            schema_version: CURRENT_SETTINGS_SCHEMA_VERSION,

            general: GeneralSettings {
                interface_language: InterfaceLanguage::English,
            },
            sidebar: SidebarPreferences::default(),
            models: ModelSettings::default(),
            content: Default::default(),
            web_search: Default::default(),
            routing: Default::default(),
        };

        let repository = Arc::new(MemorySettingsRepository::with_settings(expected.clone()));

        let manager = SettingsManager::new(repository);

        let loaded = manager.get_settings().unwrap();

        assert_eq!(loaded, expected,);
    }

    #[test]
    fn migrates_v1_settings_and_preserves_language() {
        let repository = Arc::new(MemorySettingsRepository::with_settings(AppSettings {
            schema_version: 1,
            general: GeneralSettings {
                interface_language: InterfaceLanguage::English,
            },
            sidebar: SidebarPreferences::default(),
            models: ModelSettings::default(),
            content: Default::default(),
            web_search: Default::default(),
            routing: Default::default(),
        }));
        let manager = SettingsManager::new(repository.clone());
        let loaded = manager.get_settings().unwrap();
        assert_eq!(loaded.schema_version, CURRENT_SETTINGS_SCHEMA_VERSION);
        assert_eq!(
            loaded.general.interface_language,
            InterfaceLanguage::English
        );
        assert_eq!(repository.snapshot(), Some(loaded));
    }

    #[test]
    fn updates_sidebar_preferences_and_persists_result() {
        let repository = Arc::new(MemorySettingsRepository::default());
        let manager = SettingsManager::new(repository.clone());
        let preferences = SidebarPreferences {
            standard_chats_collapsed: true,
            projects_collapsed: false,
            collapsed_chat_group_ids: vec!["g1".to_string()],
            collapsed_project_ids: vec!["p1".to_string()],
            width_px: 336,
        };
        let updated = manager
            .update_sidebar_preferences(preferences.clone())
            .unwrap();
        assert_eq!(updated.sidebar, preferences);
        assert_eq!(repository.snapshot().unwrap().sidebar, preferences);
    }

    #[test]
    fn migrates_v2_settings_with_default_sidebar_width() {
        let old = AppSettings {
            schema_version: 2,
            sidebar: SidebarPreferences {
                width_px: 0,
                collapsed_chat_group_ids: vec!["g1".to_string()],
                ..SidebarPreferences::default()
            },
            ..AppSettings::default()
        };
        let repository = Arc::new(MemorySettingsRepository::with_settings(old));
        let manager = SettingsManager::new(repository.clone());
        let loaded = manager.get_settings().unwrap();
        assert_eq!(loaded.schema_version, CURRENT_SETTINGS_SCHEMA_VERSION);
        assert_eq!(loaded.sidebar.width_px, DEFAULT_SIDEBAR_WIDTH_PX);
        assert_eq!(loaded.sidebar.collapsed_chat_group_ids, vec!["g1"]);
        assert_eq!(repository.snapshot(), Some(loaded));
    }

    #[test]
    fn migrates_v3_settings_with_default_model_configuration() {
        let old = AppSettings {
            schema_version: 3,
            models: ModelSettings {
                providers: Vec::new(),
                default_provider_profile_id: String::new(),
                default_model_id: String::new(),
                connect_timeout_secs: 0,
                first_output_timeout_secs: 0,
                chunk_timeout_secs: 0,
                total_timeout_secs: 0,
            },
            ..AppSettings::default()
        };
        let repository = Arc::new(MemorySettingsRepository::with_settings(old));
        let manager = SettingsManager::new(repository.clone());
        let loaded = manager.get_settings().unwrap();
        assert_eq!(loaded.schema_version, CURRENT_SETTINGS_SCHEMA_VERSION);
        assert_eq!(loaded.models.default_provider_profile_id, "builtin-demo");
        assert_eq!(loaded.models.default_model_id, "demo");
        assert_eq!(repository.snapshot(), Some(loaded));
    }

    #[test]
    fn migrates_v9_with_versioned_image_capability_declarations() {
        let mut old = AppSettings {
            schema_version: 9,
            ..AppSettings::default()
        };
        let model = &mut old.models.providers[0].models[0];
        model.capabilities.vision = "unknown".to_string();
        model.capabilities.vision_source = "unknown".to_string();
        model.capabilities.vision_status = "unknown".to_string();
        let repository = Arc::new(MemorySettingsRepository::with_settings(old));
        let manager = SettingsManager::new(repository.clone());
        let loaded = manager.get_settings().unwrap();
        let vision = &loaded.models.providers[0].models[0].capabilities;
        assert_eq!(loaded.schema_version, CURRENT_SETTINGS_SCHEMA_VERSION);
        assert_eq!(vision.vision, "yes");
        assert_eq!(vision.vision_source, "official_manifest");
        assert_eq!(vision.vision_status, "declared");
        assert_eq!(repository.snapshot(), Some(loaded));
    }

    #[test]
    fn user_image_confirmation_can_be_reset_to_the_manifest() {
        let repository = Arc::new(MemorySettingsRepository::default());
        let manager = SettingsManager::new(repository);
        let confirmed = manager
            .update_model_vision_confirmation("builtin-demo", "demo", Some(true))
            .unwrap();
        assert_eq!(
            confirmed.models.providers[0].models[0]
                .capabilities
                .vision_source,
            "user_confirmed"
        );
        let reset = manager
            .update_model_vision_confirmation("builtin-demo", "demo", None)
            .unwrap();
        assert_eq!(
            reset.models.providers[0].models[0]
                .capabilities
                .vision_source,
            "official_manifest"
        );
    }

    #[test]
    fn repairs_overbroad_deepseek_vision_and_search_combination_error() {
        let mut settings = AppSettings::default();
        let (_, native_search) =
            native_search_binding_for_profile("deepseek", "https://api.deepseek.com");
        settings.models.providers.push(ProviderProfileSettings {
            id: "deepseek-test".to_string(),
            name: "DeepSeek".to_string(),
            kind: "openai_compatible".to_string(),
            preset_id: "deepseek".to_string(),
            entered_base_url: "https://api.deepseek.com".to_string(),
            resolved_api_base_url: Some("https://api.deepseek.com".to_string()),
            endpoint_mode: "preset".to_string(),
            enabled: true,
            credential_ref: Some("credential-ref".to_string()),
            models: vec![ProviderModelSettings {
                id: "deepseek-v4-pro".to_string(),
                source: "manual".to_string(),
                status: "available".to_string(),
                enabled: true,
                allow_automatic_routing: true,
                tool_mode: "native".to_string(),
                capabilities: ModelCapabilities {
                    vision: "yes".to_string(),
                    vision_source: "official_manifest".to_string(),
                    vision_status: "declared".to_string(),
                    ..Default::default()
                },
                capability_states: Vec::new(),
                last_seen_at: None,
                native_web_search_status: "temporary_error".to_string(),
                native_web_search_error_code: Some("image_format_unsupported".to_string()),
                native_web_search_verified_at: None,
                route_capabilities: Vec::new(),
                tool_contracts: Vec::new(),
            }],
            last_discovered_at: None,
            primary_protocol: "openai_chat".to_string(),
            native_search,
            invocation_routes: Vec::new(),
        });

        assert!(SettingsManager::repair_multimodal_capabilities(
            &mut settings
        ));

        let model = &settings.models.providers[1].models[0];
        assert_eq!(model.capabilities.vision, "unknown");
        assert_eq!(model.capabilities.vision_source, "unknown");
        assert_eq!(model.capabilities.vision_status, "unknown");
        assert_eq!(model.native_web_search_status, "declared");
        assert_eq!(model.native_web_search_error_code, None);
    }

    #[test]
    fn v11_migration_resets_only_official_deepseek_search_rejections() {
        let mut old = AppSettings {
            schema_version: 11,
            ..AppSettings::default()
        };
        let mut official = old.models.providers[0].clone();
        official.id = "official-deepseek".to_string();
        official.preset_id = "custom".to_string();
        official.entered_base_url = "https://api.deepseek.com".to_string();
        official.resolved_api_base_url = Some("https://api.deepseek.com".to_string());
        official.primary_protocol = "openai_chat".to_string();
        official.invocation_routes.clear();
        official.models[0].native_web_search_status = "unsupported".to_string();
        official.models[0].native_web_search_error_code =
            Some("native_search_protocol_unsupported".to_string());
        official.models[0].route_capabilities.clear();

        let mut custom = official.clone();
        custom.id = "custom-gateway".to_string();
        custom.entered_base_url = "https://gateway.example/api".to_string();
        custom.resolved_api_base_url = Some("https://gateway.example/api".to_string());
        custom.invocation_routes =
            invocation_routes_for_profile("deepseek", "https://api.deepseek.com", None);
        for route in &mut custom.invocation_routes {
            route.source = "manual".to_string();
        }
        custom.models[0].route_capabilities =
            route_capabilities_for_model(&custom.invocation_routes, "unsupported");
        if let Some(state) = custom.models[0]
            .route_capabilities
            .iter_mut()
            .find(|state| {
                state.route_id == "deepseek.anthropic_web"
                    && state
                        .capability_signature
                        .iter()
                        .any(|value| value == "native_web_search")
            })
        {
            state.error_code = Some("native_search_protocol_unsupported".to_string());
        }
        old.models.providers = vec![official, custom];

        let repository = Arc::new(MemorySettingsRepository::with_settings(old));
        let loaded = SettingsManager::new(repository).get_settings().unwrap();
        let official_model = &loaded.models.providers[0].models[0];
        let official_search = official_model
            .route_capabilities
            .iter()
            .find(|state| {
                state.route_id == "deepseek.anthropic_web"
                    && state
                        .capability_signature
                        .iter()
                        .any(|value| value == "native_web_search")
            })
            .unwrap();
        assert_eq!(official_search.status, "declared");
        assert_eq!(official_search.error_code, None);

        let custom_model = &loaded.models.providers[1].models[0];
        let custom_search = custom_model
            .route_capabilities
            .iter()
            .find(|state| {
                state.route_id == "deepseek.anthropic_web"
                    && state
                        .capability_signature
                        .iter()
                        .any(|value| value == "native_web_search")
            })
            .unwrap();
        assert_eq!(custom_search.status, "unsupported");
        assert_eq!(
            custom_search.error_code.as_deref(),
            Some("native_search_protocol_unsupported")
        );
    }

    #[test]
    fn v12_serialization_has_one_route_source_of_truth() {
        let value = serde_json::to_value(AppSettings::default()).unwrap();
        let provider = &value["models"]["providers"][0];
        assert!(provider.get("invocation_routes").is_some());
        assert!(provider.get("primary_protocol").is_none());
        assert!(provider.get("native_search").is_none());
        let model = &provider["models"][0];
        assert!(model.get("route_capabilities").is_some());
        assert!(model.get("native_web_search_status").is_none());
    }

    #[test]
    fn updates_only_sidebar_width_and_validates_bounds() {
        let repository = Arc::new(MemorySettingsRepository::default());
        let manager = SettingsManager::new(repository.clone());
        let updated = manager.update_sidebar_width(360).unwrap();
        assert_eq!(updated.sidebar.width_px, 360);
        assert!(manager.update_sidebar_width(100).is_err());
        assert_eq!(repository.snapshot().unwrap().sidebar.width_px, 360);
    }

    #[test]
    fn updates_interface_language_and_persists_result() {
        let repository = Arc::new(MemorySettingsRepository::default());

        let manager = SettingsManager::new(repository.clone());

        let updated = manager
            .update_interface_language(InterfaceLanguage::SimplifiedChinese)
            .unwrap();

        assert_eq!(
            updated.general.interface_language,
            InterfaceLanguage::SimplifiedChinese,
        );

        assert_eq!(
            repository.snapshot().unwrap().general.interface_language,
            InterfaceLanguage::SimplifiedChinese,
        );
    }

    #[test]
    fn rejects_unknown_schema_versions_instead_of_overwriting_them() {
        let repository = Arc::new(MemorySettingsRepository::with_settings(AppSettings {
            schema_version: 99,

            general: GeneralSettings::default(),
            sidebar: SidebarPreferences::default(),
            models: ModelSettings::default(),
            content: Default::default(),
            web_search: Default::default(),
            routing: Default::default(),
        }));

        let manager = SettingsManager::new(repository);

        let error = manager.get_settings().unwrap_err();

        assert!(matches!(error, SettingsError::UnsupportedSchemaVersion(99)));
    }

    #[test]
    fn migrates_v4_openai_provider_without_guessing_a_new_address() {
        let mut old = AppSettings {
            schema_version: 4,
            ..AppSettings::default()
        };
        old.models.providers.push(ProviderProfileSettings {
            id: "legacy".to_string(),
            name: "Legacy".to_string(),
            kind: "openai_compatible".to_string(),
            preset_id: "custom".to_string(),
            entered_base_url: "https://gateway.example/api".to_string(),
            resolved_api_base_url: None,
            endpoint_mode: "exact".to_string(),
            enabled: false,
            credential_ref: Some("windows-credential-manager".to_string()),
            models: Vec::new(),
            last_discovered_at: None,
            primary_protocol: "openai_chat".to_string(),
            native_search: NativeSearchBinding::default(),
            invocation_routes: Vec::new(),
        });
        let repository = Arc::new(MemorySettingsRepository::with_settings(old));
        let manager = SettingsManager::new(repository);
        let loaded = manager.get_settings().unwrap();
        let legacy = loaded
            .models
            .providers
            .iter()
            .find(|item| item.id == "legacy")
            .unwrap();
        assert_eq!(legacy.endpoint_mode, "legacy_auto_v1");
        assert_eq!(legacy.entered_base_url, "https://gateway.example/api");
        assert_eq!(
            legacy.credential_ref.as_deref(),
            Some("windows-credential-manager")
        );
    }

    #[test]
    fn migrates_v5_with_remote_images_blocked_by_default() {
        let old = AppSettings {
            schema_version: 5,
            ..AppSettings::default()
        };
        let repository = Arc::new(MemorySettingsRepository::with_settings(old));
        let manager = SettingsManager::new(repository.clone());

        let loaded = manager.get_settings().unwrap();

        assert_eq!(loaded.schema_version, CURRENT_SETTINGS_SCHEMA_VERSION);
        assert_eq!(loaded.content.remote_image_policy, RemoteImagePolicy::Ask);
        assert_eq!(repository.snapshot(), Some(loaded));
    }

    #[test]
    fn updates_remote_image_policy_and_persists_result() {
        let repository = Arc::new(MemorySettingsRepository::default());
        let manager = SettingsManager::new(repository.clone());

        let updated = manager
            .update_remote_image_policy(RemoteImagePolicy::Allow)
            .unwrap();

        assert_eq!(
            updated.content.remote_image_policy,
            RemoteImagePolicy::Allow
        );
        assert_eq!(repository.snapshot(), Some(updated));
    }

    #[test]
    fn migrates_v6_model_limits_to_conservative_fallbacks() {
        let old = AppSettings {
            schema_version: 6,
            ..AppSettings::default()
        };
        let repository = Arc::new(MemorySettingsRepository::with_settings(old));
        let manager = SettingsManager::new(repository.clone());

        let loaded = manager.get_settings().unwrap();
        let demo = &loaded.models.providers[0].models[0];

        assert_eq!(loaded.schema_version, CURRENT_SETTINGS_SCHEMA_VERSION);
        assert_eq!(demo.capabilities.max_output_tokens, None);
        assert_eq!(demo.capabilities.context_length_source, "fallback");
        assert_eq!(repository.snapshot(), Some(loaded));
    }

    #[test]
    fn updates_and_validates_manual_model_context_limits() {
        let repository = Arc::new(MemorySettingsRepository::default());
        let manager = SettingsManager::new(repository.clone());

        let updated = manager
            .update_model_context_limits("builtin-demo", "demo", Some(32_768), Some(4_096))
            .unwrap();
        let demo = &updated.models.providers[0].models[0];
        assert_eq!(demo.capabilities.context_length, Some(32_768));
        assert_eq!(demo.capabilities.max_output_tokens, Some(4_096));
        assert_eq!(demo.capabilities.context_length_source, "manual");
        assert!(manager
            .update_model_context_limits("builtin-demo", "demo", Some(1_024), Some(1_024))
            .is_err());
        assert_eq!(repository.snapshot(), Some(updated));
    }

    #[test]
    fn detected_models_remain_disabled_and_refresh_preserves_admission() {
        let repository = Arc::new(MemorySettingsRepository::default());
        let manager = SettingsManager::new(repository);
        manager
            .upsert_provider(ProviderProfileSettings {
                id: "remote".to_string(),
                name: "Remote".to_string(),
                kind: "openai_compatible".to_string(),
                preset_id: "custom".to_string(),
                entered_base_url: "https://example.com/v1".to_string(),
                resolved_api_base_url: Some("https://example.com/v1".to_string()),
                endpoint_mode: "exact".to_string(),
                enabled: false,
                credential_ref: None,
                models: Vec::new(),
                last_discovered_at: None,
                primary_protocol: "openai_chat".to_string(),
                native_search: NativeSearchBinding::default(),
                invocation_routes: Vec::new(),
            })
            .unwrap();
        let detected = || {
            vec![ProviderModelSettings {
                id: "model-a".to_string(),
                source: "discovered".to_string(),
                status: "available".to_string(),
                enabled: false,
                allow_automatic_routing: false,
                tool_mode: "unknown".to_string(),
                capabilities: ModelCapabilities::default(),
                capability_states: Vec::new(),
                last_seen_at: Some("now".to_string()),
                native_web_search_status: "not_applicable".to_string(),
                native_web_search_error_code: None,
                native_web_search_verified_at: None,
                route_capabilities: Vec::new(),
                tool_contracts: Vec::new(),
            }]
        };
        let first = manager
            .replace_provider_catalog("remote", detected(), None)
            .unwrap();
        assert!(
            !first
                .models
                .providers
                .iter()
                .find(|item| item.id == "remote")
                .unwrap()
                .models[0]
                .enabled
        );
        manager
            .set_provider_model_enabled("remote", "model-a", true)
            .unwrap();
        manager
            .update_model_context_limits("remote", "model-a", Some(65_536), Some(4_096))
            .unwrap();
        let refreshed = manager
            .replace_provider_catalog("remote", detected(), None)
            .unwrap();
        let refreshed_model = &refreshed
            .models
            .providers
            .iter()
            .find(|item| item.id == "remote")
            .unwrap()
            .models[0];
        assert!(refreshed_model.enabled);
        assert_eq!(refreshed_model.capabilities.context_length, Some(65_536));
        assert_eq!(refreshed_model.capabilities.max_output_tokens, Some(4_096));
        assert_eq!(refreshed_model.capabilities.context_length_source, "manual");
    }

    #[test]
    fn current_default_cannot_be_disabled() {
        let repository = Arc::new(MemorySettingsRepository::default());
        let manager = SettingsManager::new(repository);
        let error = manager
            .set_provider_model_enabled("builtin-demo", "demo", false)
            .unwrap_err();
        assert!(matches!(error, SettingsError::InvalidData(_)));
        let error = manager.remove_provider("builtin-demo").unwrap_err();
        assert!(matches!(error, SettingsError::InvalidData(_)));
    }

    #[test]
    fn search_backend_health_is_persisted_without_exposing_credentials() {
        let repository = Arc::new(MemorySettingsRepository::default());
        let manager = SettingsManager::new(repository.clone());
        manager
            .upsert_search_backend(SearchBackendSettings {
                id: "search-1".to_string(),
                name: "Search".to_string(),
                kind: "searxng".to_string(),
                base_url: "https://search.example".to_string(),
                enabled: true,
                credential_ref: None,
                last_status: None,
                last_tested_at: None,
            })
            .unwrap();
        let tested_at = "2026-09-17T12:00:00Z";

        let updated = manager
            .update_search_backend_status("search-1", "available", tested_at)
            .unwrap();

        let backend = &updated.web_search.backends[0];
        assert_eq!(backend.last_status.as_deref(), Some("available"));
        assert_eq!(backend.last_tested_at.as_deref(), Some(tested_at));
        assert_eq!(repository.snapshot(), Some(updated));
    }

    #[test]
    fn migrates_v7_with_web_search_disabled_and_preserves_existing_settings() {
        let mut old = AppSettings {
            schema_version: 7,
            ..AppSettings::default()
        };
        old.general.interface_language = InterfaceLanguage::English;
        old.sidebar.width_px = 344;
        old.web_search.enabled = true;
        let repository = Arc::new(MemorySettingsRepository::with_settings(old));
        let manager = SettingsManager::new(repository.clone());

        let loaded = manager.get_settings().unwrap();

        assert_eq!(loaded.schema_version, CURRENT_SETTINGS_SCHEMA_VERSION);
        assert_eq!(
            loaded.general.interface_language,
            InterfaceLanguage::English
        );
        assert_eq!(loaded.sidebar.width_px, 344);
        assert!(!loaded.web_search.enabled);
        assert!(!loaded.web_search.privacy_acknowledged);
        assert_eq!(repository.snapshot(), Some(loaded));
    }

    #[test]
    fn migrates_v10_with_routing_defaults_and_preserves_model_and_search_settings() {
        let mut old = AppSettings {
            schema_version: 10,
            ..AppSettings::default()
        };
        old.general.interface_language = InterfaceLanguage::English;
        old.web_search.enabled = true;
        old.web_search.privacy_acknowledged = true;
        let provider_count = old.models.providers.len();
        let repository = Arc::new(MemorySettingsRepository::with_settings(old));
        let manager = SettingsManager::new(repository.clone());

        let loaded = manager.get_settings().unwrap();

        assert_eq!(loaded.schema_version, CURRENT_SETTINGS_SCHEMA_VERSION);
        assert_eq!(
            loaded.general.interface_language,
            InterfaceLanguage::English
        );
        assert!(loaded.web_search.enabled);
        assert!(loaded.web_search.privacy_acknowledged);
        assert_eq!(loaded.models.providers.len(), provider_count);
        assert_eq!(loaded.routing.new_conversation_mode, "automatic");
        assert_eq!(loaded.routing.default_preset, "balanced");
        assert_eq!(loaded.routing.max_model_calls_per_message, 3);
        assert_eq!(repository.snapshot(), Some(loaded));
    }

    #[test]
    fn migrates_v14_source_provenance_for_official_routes_only() {
        let mut old = AppSettings {
            schema_version: 14,
            ..AppSettings::default()
        };
        let mut official = old.models.providers[0].clone();
        official.id = "deepseek-official".to_string();
        official.preset_id = "deepseek".to_string();
        official.endpoint_mode = "preset".to_string();
        official.entered_base_url = "https://api.deepseek.com".to_string();
        official.invocation_routes =
            invocation_routes_for_profile("deepseek", "https://api.deepseek.com", None);
        for route in &mut official.invocation_routes {
            route.source_provenance = "unknown".to_string();
        }
        let mut custom = official.clone();
        custom.id = "custom-gateway".to_string();
        custom.preset_id = "custom".to_string();
        custom.endpoint_mode = "custom".to_string();
        custom.entered_base_url = "https://gateway.example/v1".to_string();
        custom.invocation_routes =
            invocation_routes_for_profile("custom", "https://gateway.example/v1", None);
        old.models.providers = vec![official, custom];
        let repository = Arc::new(MemorySettingsRepository::with_settings(old));
        let manager = SettingsManager::new(repository);

        let loaded = manager.get_settings().unwrap();
        let official = loaded
            .models
            .providers
            .iter()
            .find(|profile| profile.id == "deepseek-official")
            .unwrap();
        let custom = loaded
            .models
            .providers
            .iter()
            .find(|profile| profile.id == "custom-gateway")
            .unwrap();
        assert_eq!(
            official
                .invocation_routes
                .iter()
                .find(|route| route.adapter_id == "deepseek.anthropic_web")
                .unwrap()
                .source_provenance,
            "structured",
        );
        assert!(custom
            .invocation_routes
            .iter()
            .all(|route| route.source_provenance == "unknown"));
    }

    #[test]
    fn migrates_v15_temporary_route_failures_without_blocking_capabilities() {
        let mut old = AppSettings {
            schema_version: 15,
            ..AppSettings::default()
        };
        let mut official = old.models.providers[0].clone();
        official.id = "deepseek".to_string();
        official.kind = "openai_compatible".to_string();
        official.preset_id = "deepseek".to_string();
        official.entered_base_url = "https://api.deepseek.com".to_string();
        official.invocation_routes =
            invocation_routes_for_profile("deepseek", "https://api.deepseek.com", None);
        let mut flash = official.models[0].clone();
        flash.id = "deepseek-flash".to_string();
        flash.capabilities.vision_status = "temporary_error".to_string();
        flash.capabilities.vision_error_code = Some("provider_empty_output".to_string());
        flash.route_capabilities =
            route_capabilities_for_model(&official.invocation_routes, "declared");
        for state in &mut flash.route_capabilities {
            if state
                .capability_signature
                .iter()
                .any(|value| value == "vision")
            {
                state.status = "temporary_error".to_string();
                state.error_code = Some("provider_empty_output".to_string());
            }
        }
        official.models = vec![flash];
        old.models.providers = vec![official];
        let repository = Arc::new(MemorySettingsRepository::with_settings(old));
        let loaded = SettingsManager::new(repository).get_settings().unwrap();
        let model = &loaded.models.providers[0].models[0];
        assert_ne!(model.capabilities.vision_status, "temporary_error");
        assert!(model
            .route_capabilities
            .iter()
            .all(|state| { state.status != "temporary_error" && state.error_code.is_none() }));
        assert!(model.route_capabilities.iter().any(|state| {
            state
                .capability_signature
                .iter()
                .any(|value| value == "vision")
                && state.status == "declared"
        }));
    }

    #[test]
    fn repairs_native_search_adapter_for_existing_official_provider() {
        let mut current = AppSettings {
            schema_version: 8,
            ..AppSettings::default()
        };
        let mut profile = current.models.providers[0].clone();
        profile.id = "existing-kimi".to_string();
        profile.name = "Existing Kimi".to_string();
        profile.kind = "openai_compatible".to_string();
        profile.preset_id = "moonshot_cn".to_string();
        profile.entered_base_url = "https://api.moonshot.cn/v1".to_string();
        profile.resolved_api_base_url = Some(profile.entered_base_url.clone());
        profile.native_search = NativeSearchBinding::default();
        profile.primary_protocol = "openai_chat".to_string();
        profile.models[0].native_web_search_status = "unsupported".to_string();
        profile.models[0].native_web_search_verified_at = Some("stale".to_string());
        current.models.providers.push(profile);
        let repository = Arc::new(MemorySettingsRepository::with_settings(current));
        let manager = SettingsManager::new(repository.clone());

        let loaded = manager.get_settings().unwrap();
        let repaired = loaded
            .models
            .providers
            .iter()
            .find(|item| item.id == "existing-kimi")
            .unwrap();

        assert_eq!(repaired.native_search.adapter_id, "kimi_builtin_web_search");
        assert_eq!(repaired.models[0].native_web_search_status, "declared");
        assert_eq!(repaired.models[0].native_web_search_verified_at, None);
        assert_eq!(repository.snapshot(), Some(loaded));
    }
    #[test]
    fn managed_default_persists_only_stable_references() {
        let repository = Arc::new(MemorySettingsRepository::default());
        let manager = SettingsManager::new(repository.clone());
        let id = "a".repeat(64);
        assert!(manager
            .update_managed_default("managed:spoof", &id)
            .is_err());
        manager
            .update_managed_default(&format!("managed:{id}"), &id)
            .unwrap();
        let stored = repository.snapshot().unwrap();
        assert_eq!(stored.models.default_model_id, id);
        assert_eq!(
            stored.models.default_provider_profile_id,
            format!("managed:{id}")
        );
        assert!(!stored
            .models
            .providers
            .iter()
            .any(|profile| profile.id.starts_with("managed:")));
        assert_eq!(manager.get_settings().unwrap(), stored);
    }
}
