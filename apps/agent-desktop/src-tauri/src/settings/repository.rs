use std::sync::Arc;

use tauri::{AppHandle, Wry};

use tauri_plugin_store::{Store, StoreExt};

use super::{
    error::SettingsError,
    model::{AppSettings, GeneralSettings, ModelSettings, SidebarPreferences},
};

const SETTINGS_STORE_PATH: &str = "settings.json";
const SETTINGS_KEY: &str = "settings";

const SCHEMA_VERSION_KEY: &str = "schema_version";

const GENERAL_KEY: &str = "general";

fn explicit_store<R: tauri::Runtime>(
    app: &AppHandle<R>,
    path: impl AsRef<std::path::Path>,
) -> Result<Arc<Store<R>>, SettingsError> {
    let store = app
        .store_builder(path)
        .disable_auto_save()
        .build()
        .map_err(|error| {
            SettingsError::Storage(format!("failed to open settings store: {error}"))
        })?;
    // All writes are explicit. Detach the resource so plugin Exit cannot save
    // an obsolete cache after migration or update recovery replaced the file.
    store.close_resource();
    Ok(store)
}

fn decode_settings_value(value: serde_json::Value) -> Result<AppSettings, SettingsError> {
    serde_json::from_value::<AppSettings>(value)
        .map_err(|error| SettingsError::Serialization(format!("invalid settings object: {error}")))
}

/// SettingsManager 所依赖的持久化接口。
///
/// Manager 不知道数据最终存在 JSON、SQLite、云同步还是其他地方。
/// 当前正式桌面实现由 Tauri Store 提供。
pub trait SettingsRepository: Send + Sync {
    fn load(&self) -> Result<Option<AppSettings>, SettingsError>;

    fn save(&self, settings: &AppSettings) -> Result<(), SettingsError>;
}

/// agent 当前正式设置存储实现。
///
/// Store 由 Rust Host 持有，React 不直接访问它。
pub struct TauriStoreSettingsRepository {
    store: Arc<Store<Wry>>,
}

impl TauriStoreSettingsRepository {
    pub fn new(app: &AppHandle) -> Result<Self, SettingsError> {
        /*
         * 关闭 Store 自带的逐次自动保存。
         *
         * SettingsRepository 会在一次逻辑更新完成以后主动 save，
         * 避免 schema_version 和 general 分别触发落盘。
         */
        let store = explicit_store(app, SETTINGS_STORE_PATH)?;

        Ok(Self { store })
    }
}

impl SettingsRepository for TauriStoreSettingsRepository {
    fn load(&self) -> Result<Option<AppSettings>, SettingsError> {
        if let Some(settings_value) = self.store.get(SETTINGS_KEY) {
            let settings = decode_settings_value(settings_value)?;
            return Ok(Some(settings));
        }

        // Legacy Stage 1.5 layout. It is migrated to the root object on the next save.
        let schema_value = self.store.get(SCHEMA_VERSION_KEY);

        let general_value = self.store.get(GENERAL_KEY);

        match (schema_value, general_value) {
            /*
             * 两个核心字段都不存在说明这是第一次运行。
             *
             * 这里返回 None，让 SettingsManager 决定默认配置，
             * Repository 不应该包含产品默认值逻辑。
             */
            (None, None) => Ok(None),

            (Some(schema_value), Some(general_value)) => {
                let schema_version =
                    serde_json::from_value::<u32>(schema_value).map_err(|error| {
                        SettingsError::Serialization(format!("invalid schema_version: {error}"))
                    })?;

                let general =
                    serde_json::from_value::<GeneralSettings>(general_value).map_err(|error| {
                        SettingsError::Serialization(format!("invalid general settings: {error}"))
                    })?;

                Ok(Some(AppSettings {
                    schema_version,
                    general,
                    sidebar: SidebarPreferences::default(),
                    models: ModelSettings::default(),
                    content: Default::default(),
                    web_search: Default::default(),
                    routing: Default::default(),
                }))
            }

            /*
             * 只存在一半设置通常意味着文件损坏或写入被外部程序打断。
             *
             * 不静默覆盖用户文件，否则真正的数据问题会被隐藏。
             */
            _ => Err(SettingsError::InvalidData(
                "settings store is incomplete".to_string(),
            )),
        }
    }

    fn save(&self, settings: &AppSettings) -> Result<(), SettingsError> {
        if crate::profile_migration::blocks_core() {
            return Err(SettingsError::Storage(
                "migration_activation_in_progress".into(),
            ));
        }
        let settings_value = serde_json::to_value(settings).map_err(|error| {
            SettingsError::Serialization(format!("failed to serialize settings: {error}"))
        })?;

        let previous_root = self.store.get(SETTINGS_KEY);
        let previous_schema = self.store.get(SCHEMA_VERSION_KEY);
        let previous_general = self.store.get(GENERAL_KEY);
        self.store.set(SETTINGS_KEY, settings_value);
        self.store.delete(SCHEMA_VERSION_KEY);
        self.store.delete(GENERAL_KEY);

        /*
         * Repository 自己负责明确落盘。
         *
         * 调用者只有在 save() 成功之后才能把一次设置修改
         * 视为真正完成。
         */
        if let Err(error) = self.store.save() {
            match previous_root {
                Some(value) => self.store.set(SETTINGS_KEY, value),
                None => {
                    self.store.delete(SETTINGS_KEY);
                }
            }
            match previous_schema {
                Some(value) => self.store.set(SCHEMA_VERSION_KEY, value),
                None => {
                    self.store.delete(SCHEMA_VERSION_KEY);
                }
            }
            match previous_general {
                Some(value) => self.store.set(GENERAL_KEY, value),
                None => {
                    self.store.delete(GENERAL_KEY);
                }
            }
            return Err(SettingsError::Storage(format!(
                "failed to save settings store: {error}"
            )));
        }

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::decode_settings_value;

    #[test]
    fn rejects_corrupted_root_settings() {
        assert!(decode_settings_value(json!({"schema_version": 1, "general": "broken"})).is_err());
    }

    #[test]
    fn explicit_store_cannot_overwrite_migrated_settings_during_host_exit() {
        use tauri_plugin_store::StoreExt;
        let app = tauri::test::mock_builder()
            .plugin(tauri_plugin_store::Builder::default().build())
            .build(tauri::test::mock_context(tauri::test::noop_assets()))
            .unwrap();
        let root = std::env::temp_dir().join(uuid::Uuid::new_v4().to_string());
        std::fs::create_dir(&root).unwrap();
        let path = root.join("settings.json");
        let store = super::explicit_store(app.handle(), &path).unwrap();
        store.set("settings", json!({"source":"old target cache"}));
        store.save().unwrap();
        std::fs::write(&path, br#"{"settings":{"source":"migrated"}}"#).unwrap();
        assert!(
            app.get_store(&path).is_none(),
            "Exit must have no cached settings store to save"
        );
        app.cleanup_before_exit();
        let value: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(value["settings"]["source"], "migrated");
        std::fs::remove_dir_all(root).unwrap();
    }
}
