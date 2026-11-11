use serde_json::json;
use settings::{SettingsManager, TauriStoreSettingsRepository};
use std::{
    fs,
    sync::{Arc, Mutex},
};
use tauri::Manager;
mod agent_execution;
mod app_updates;
mod attachment_processor;
mod attachment_store;
mod change_set_planner;
mod change_set_recovery;
mod commands;
mod core_bridge;
mod core_process_manager;
#[cfg(windows)]
mod core_transport;
mod credentials;
mod domain_commands;
mod file_access;
mod file_search;
mod file_security;
mod image_normalizer;
mod local_model_commands;
mod logging;
mod offline;
mod rag_commands;
#[cfg(test)]
mod reliability_contracts;
mod route_health;
mod runtime_capabilities;
mod settings;
mod skill_commands;
mod system_locale;
mod tool_event_handler;
mod web_search;
mod work_tools;
use core_process_manager::CoreProcessManager;

use logging::{events, retention::cleanup_logs, AppLogger, LogLevel};

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        // 必须尽量最先注册。
        //
        // 如果 agent 已经运行，
        // 用户再次双击应用时：
        //
        // 1. 不再启动第二个完整实例；
        // 2. 第二个实例会通知当前实例；
        // 3. 当前主窗口被显示并获得焦点。
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_store::Builder::default().build())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(app_updates::AppUpdateService::default())
        .manage(Mutex::new(CoreProcessManager::new()))
        .manage(credentials::CredentialStore)
        .manage(Mutex::new(rag_commands::RagPathGrants::default()))
        .manage(Mutex::new(local_model_commands::LocalModelGrants::default()))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let app_data_dir = app.path().app_data_dir()?;
            let grants_path = app_data_dir.join("work-tool-grants.json");
            let work_tools =
                work_tools::WorkToolManager::load(grants_path.clone()).unwrap_or_else(|error| {
                    eprintln!("[agent] Project file authorization is disabled: {error}");
                    work_tools::WorkToolManager::disabled(
                        grants_path,
                        "project authorization migration failed",
                    )
                });
            app.manage(Mutex::new(work_tools));
            let credential_store = app.state::<credentials::CredentialStore>();
            let recovery_credential_id = "__installation_recovery_key_v1";
            let recovery_dir = app_data_dir.join("file-recovery");
            let execution_result =
                (|| -> Result<agent_execution::AgentExecutionManager, String> {
                    let recovery_secret = match credential_store.get(recovery_credential_id)? {
                        Some(secret) => secret,
                        None => {
                            let secret = uuid::Uuid::new_v4().to_string();
                            credential_store.set(recovery_credential_id, &secret)?;
                            secret
                        }
                    };
                    agent_execution::AgentExecutionManager::new(
                        recovery_dir.clone(),
                        &recovery_secret,
                    )
                })();
            let execution = execution_result.unwrap_or_else(|error| {
                eprintln!("[agent] Secure file tools are disabled: {error}");
                agent_execution::AgentExecutionManager::disabled(
                    recovery_dir,
                    "secure recovery initialization failed",
                )
            });
            app.manage(Mutex::new(execution));
            app.manage(Mutex::new(attachment_store::AttachmentStore::new(
                app_data_dir.clone(),
            )?));
            app.manage(Mutex::new(route_health::RouteHealthRegistry::load(
                app_data_dir.join("route-health-v2.json"),
            )?));
            // =====================================================
            // 1. 初始化 Rust 全局日志系统
            // =====================================================

            let debug_enabled = cfg!(debug_assertions);

            let logger = match app.path().app_log_dir() {
                Ok(log_dir) => {
                    match fs::create_dir_all(&log_dir) {
                        Ok(()) => {
                            // =================================
                            // 在任何日志文件被打开之前
                            // 执行统一日志保留策略。
                            //
                            // 策略：
                            // - 保留最近 14 天；
                            // - 总日志最多 200 MB；
                            // - 只处理 host/core/ipc JSONL。
                            // =================================

                            let retention_result = cleanup_logs(&log_dir);

                            // Retention 必须先执行，
                            // 然后才创建 AppLogger。
                            //
                            // 这样 Windows 下不会因为当前日志文件
                            // 已经被打开而导致旧日志无法删除。
                            let logger = AppLogger::new(log_dir, debug_enabled);

                            match retention_result {
                                Ok(result) => {
                                    logger.host(
                                        LogLevel::Info,
                                        events::LOG_RETENTION_COMPLETED,
                                        "Log retention completed",
                                        json!({
                                            "deleted_files":
                                                result.deleted_files,

                                            "failed_files":
                                                result.failed_files,

                                            "freed_bytes":
                                                result.freed_bytes,

                                            "remaining_bytes":
                                                result.remaining_bytes
                                        }),
                                    );
                                }

                                Err(error) => {
                                    // 日志清理失败属于降级，
                                    // 不能阻止 agent 启动。
                                    logger.host(
                                        LogLevel::Warn,
                                        events::LOGGING_DEGRADED,
                                        "Log retention failed",
                                        json!({
                                            "error_type":
                                                "retention_failed",

                                            "error":
                                                error
                                        }),
                                    );
                                }
                            }

                            logger
                        }

                        Err(error) => {
                            // 系统日志目录无法创建时，
                            // 自动降级为 stderr-only。
                            //
                            // 日志基础设施失败不能导致
                            // agent 主程序无法启动。
                            eprintln!("[agent] Failed to create log directory: {error}");

                            AppLogger::stderr_only(debug_enabled)
                        }
                    }
                }

                Err(error) => {
                    // 连系统日志目录都无法解析时，
                    // 同样退化为 stderr-only。
                    eprintln!("[agent] Failed to resolve log directory: {error}");

                    AppLogger::stderr_only(debug_enabled)
                }
            };
            /*
             * SettingsManager 是应用级持久配置的唯一权威入口。
             *
             * Store plugin 已在 Builder 上完成初始化，
             * 因此这里可以安全加载 settings.json。
             */
            let settings_repository = TauriStoreSettingsRepository::new(app.handle())?;

            let settings_manager = SettingsManager::new(Arc::new(settings_repository));

            /*
             * 第一次运行时立即创建默认配置；
             * 已有配置则验证 schema。
             *
             * 如果设置文件损坏或版本不受支持，
             * 当前阶段选择明确启动失败，而不是静默覆盖用户配置。
             */
            settings_manager.ensure_initialized()?;

            app.manage(settings_manager);
            app.manage(web_search::WebSearchService::default());
            app.manage(file_search::FileSearchService::default());
            app.manage(terminal_service::TerminalService::default());
            // =====================================================
            // 2. 注册全局 AppLogger
            // =====================================================
            //
            // CoreProcessManager、CoreBridge 等模块
            // 后续取得的都是同一个 AppLogger。

            app.manage(logger);

            let logger = app.state::<AppLogger>();

            // =====================================================
            // 3. 记录 Rust 日志系统启动
            // =====================================================

            logger.host(
                LogLevel::Info,
                events::LOGGING_STARTED,
                "Rust logging system started",
                json!({
                    "file_enabled":
                        logger.log_dir().is_some(),

                    "debug_enabled":
                        debug_enabled
                }),
            );

            // =====================================================
            // 4. 记录 Host 生命周期启动
            // =====================================================

            logger.host(
                LogLevel::Info,
                events::HOST_STARTED,
                "agent desktop host started",
                json!({
                    "pid":
                        std::process::id()
                }),
            );

            // =====================================================
            // 5. 自动启动 Python Agent Core
            // =====================================================

            let app_handle = app.handle().clone();

            let state = app.state::<Mutex<CoreProcessManager>>();

            match state.lock() {
                Ok(mut manager) => {
                    // CoreProcessManager.start()
                    // 内部已经负责：
                    //
                    // core_start_requested
                    // core_started
                    // core_start_failed
                    //
                    // 所以这里不重复记录启动成功事件。
                    if let Err(error) = manager.start(&app_handle) {
                        // 这里保留一个 Host 层兜底日志。
                        //
                        // CoreProcessManager 自身已经记录
                        // 更具体的失败原因，
                        // 这里表示自动启动流程失败。
                        logger.host(
                            LogLevel::Error,
                            events::CORE_START_FAILED,
                            "Automatic Agent Core startup failed",
                            json!({
                                "error":
                                    error
                            }),
                        );
                    }
                }

                Err(_) => {
                    logger.host(
                        LogLevel::Error,
                        events::CORE_START_FAILED,
                        "Failed to lock CoreProcessManager during startup",
                        json!({
                            "error_type":
                                "manager_lock_failed"
                        }),
                    );
                }
            };

            let provider_app = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                let _ = settings::commands::reinject_providers(&provider_app).await;
            });

            let cleanup_app = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                loop {
                    let retry_after = if domain_commands::purge_expired_trash(&cleanup_app)
                        .await
                        .is_ok()
                    {
                        24 * 60 * 60
                    } else {
                        60
                    };
                    tokio::time::sleep(std::time::Duration::from_secs(retry_after)).await;
                }
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            app_updates::app_update_status,
            app_updates::check_app_update,
            app_updates::download_app_update,
            app_updates::install_app_update,
            app_updates::draft::load_update_draft,
            app_updates::draft::ack_update_draft,
            skill_commands::list_skills,
            local_model_commands::local_model_request,
            local_model_commands::local_model_select_path,
            local_model_commands::local_model_revoke_path,
            rag_commands::rag_request,
            rag_commands::rag_open_original,
            rag_commands::rag_select_paths,
            rag_commands::rag_revoke_paths,
            skill_commands::save_skill,
            skill_commands::import_skill_directory,
            skill_commands::inspect_skill,
            skill_commands::set_skill_enabled,
            skill_commands::remove_skill,
            greet,
            core_bridge::ping_agent_core,
            core_bridge::start_agent_core,
            core_bridge::agent_core_status,
            core_bridge::shutdown_agent_core,
            domain_commands::list_projects,
            domain_commands::create_project,
            domain_commands::update_project,
            domain_commands::delete_project,
            domain_commands::project_delete_impact,
            domain_commands::list_chat_groups,
            domain_commands::create_chat_group,
            domain_commands::rename_chat_group,
            domain_commands::delete_chat_group,
            domain_commands::reorder_chat_groups,
            domain_commands::reorder_projects,
            domain_commands::list_conversations,
            domain_commands::get_conversation,
            domain_commands::create_conversation,
            domain_commands::update_conversation,
            domain_commands::update_conversation_rag,
            domain_commands::derive_work,
            domain_commands::reorder_conversations,
            domain_commands::move_chat,
            domain_commands::move_work,
            domain_commands::copy_work,
            domain_commands::delete_conversation,
            domain_commands::start_model_run,
            domain_commands::resolve_model_run_budget,
            domain_commands::get_model_run_plan,
            domain_commands::update_conversation_routing,
            domain_commands::update_conversation_web_search,
            domain_commands::update_project_web_access,
            domain_commands::list_web_operations,
            domain_commands::list_conversation_web_operations,
            domain_commands::preview_web_source,
            domain_commands::select_message_attachments,
            domain_commands::import_dropped_attachments,
            domain_commands::import_clipboard_image,
            domain_commands::discard_staged_attachment,
            domain_commands::preview_staged_attachment,
            domain_commands::preview_message_attachment,
            domain_commands::get_attachment_normalization_status,
            domain_commands::preview_project_file,
            domain_commands::open_project_directory,
            domain_commands::open_project_document,
            domain_commands::open_project_root_directory,
            commands::model_runs::get_model_run,
            commands::model_runs::get_conversation_model_run,
            commands::model_runs::cancel_model_run,
            commands::model_runs::retry_model_run,
            commands::context::get_run_context,
            commands::context::get_context_source,
            commands::context::continue_model_run_context,
            commands::context::get_context_compaction_status,
            commands::context::resume_context_compaction,
            commands::context::pause_context_compaction,
            domain_commands::list_provider_models,
            domain_commands::list_project_knowledge,
            domain_commands::get_global_instructions,
            domain_commands::update_global_instructions,
            domain_commands::reset_global_instructions,
            domain_commands::propose_project_knowledge,
            domain_commands::publish_project_knowledge,
            domain_commands::reject_project_knowledge,
            domain_commands::update_project_knowledge,
            domain_commands::pin_project_knowledge,
            domain_commands::archive_project_knowledge,
            domain_commands::update_conversation_model,
            domain_commands::update_project_model_policy,
            domain_commands::get_project_model_scope,
            domain_commands::update_project_model_scope,
            domain_commands::list_trash,
            domain_commands::restore_trash,
            domain_commands::permanently_delete_trash,
            domain_commands::empty_trash,
            domain_commands::choose_project_directory,
            domain_commands::get_project_grant,
            domain_commands::get_work_permission,
            domain_commands::set_work_permission,
            domain_commands::list_agent_change_sets,
            domain_commands::resolve_agent_change_set,
            domain_commands::undo_agent_change_set,
            domain_commands::recover_agent_change_set,
            domain_commands::update_work_purpose,
            domain_commands::list_tool_operations,
            domain_commands::list_conversation_tool_operations,
            terminal_service::resolve_terminal_operation,
            settings::commands::get_app_settings,
            settings::commands::update_interface_language,
            settings::commands::update_remote_image_policy,
            settings::commands::update_routing_preferences,
            settings::commands::update_sidebar_preferences,
            settings::commands::update_sidebar_width,
            settings::commands::save_model_provider,
            settings::commands::connect_model_provider,
            settings::commands::refresh_provider_models,
            settings::commands::set_provider_model_enabled,
            settings::commands::set_model_automatic_routing,
            settings::commands::probe_model_tool_capability,
            settings::commands::count_conversation_model_references,
            settings::commands::count_provider_conversation_references,
            settings::commands::add_manual_provider_model,
            settings::commands::update_model_context_limits,
            settings::commands::update_model_routing_metadata,
            settings::commands::update_model_vision_confirmation,
            settings::commands::remove_manual_provider_model,
            settings::commands::set_model_provider_enabled,
            settings::commands::delete_model_provider,
            settings::commands::update_default_model,
            commands::web_search::update_web_search_settings,
            commands::web_search::save_search_backend,
            commands::web_search::delete_search_backend,
            commands::web_search::test_search_backend,
            commands::web_search::probe_native_web_search,
            route_health::list_route_health,
            route_health::reset_route_health,
            system_locale::commands::get_system_locale,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Tauri application");

    // =========================================================
    // 应用主事件循环
    // =========================================================

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            let logger = app_handle.state::<AppLogger>();

            // =================================================
            // 6. 应用退出时关闭 Python Core
            // =================================================

            let state = app_handle.state::<Mutex<CoreProcessManager>>();

            logger.host(
                LogLevel::Info,
                events::CORE_STOP_REQUESTED,
                "Agent Core stop requested during application exit",
                json!({
                    "reason":
                        "application_exit"
                }),
            );

            match state.lock() {
                Ok(mut manager) => {
                    app_handle
                        .state::<file_search::FileSearchService>()
                        .invalidate_all();
                    if let Err(error) = manager.shutdown() {
                        logger.host(
                            LogLevel::Error,
                            events::CORE_STOP_FAILED,
                            "Failed to stop Agent Core during application exit",
                            json!({
                                "error":
                                    error,

                                "reason":
                                    "application_exit"
                            }),
                        );
                    } else {
                        logger.host(
                            LogLevel::Info,
                            events::CORE_STOPPED,
                            "Agent Core shutdown command completed during application exit",
                            json!({
                                "reason":
                                    "application_exit"
                            }),
                        );
                    }
                }

                Err(_) => {
                    logger.host(
                        LogLevel::Error,
                        events::CORE_STOP_FAILED,
                        "Failed to lock CoreProcessManager during application exit",
                        json!({
                            "error_type":
                                "manager_lock_failed",

                            "reason":
                                "application_exit"
                        }),
                    );
                }
            };

            // =================================================
            // 7. Host 生命周期结束
            // =================================================

            logger.host(
                LogLevel::Info,
                events::HOST_STOPPED,
                "agent desktop host stopped",
                json!({
                    "pid":
                        std::process::id()
                }),
            );
        }
    });
}

mod terminal_executor;
#[cfg(windows)]
mod offline_terminal;
mod terminal_service;
