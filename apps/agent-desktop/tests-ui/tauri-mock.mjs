export async function installTauriMock(page, overrides = {}) {
  const now = "2026-09-15T00:00:00+00:00";
  const project = {
    id: "project-1", name: "Project Alpha", category: "coding", sort_order: 0,
    created_at: now, updated_at: now, deleted_at: null,
  };
  const work = {
    id: "work-1", kind: "project_work", title: "Implementation work", category: null,
    resolved_category: "coding", chat_group_id: null, project_id: project.id, sort_order: 0,
    derived_from_conversation_id: null, derived_through_message_id: null, derived_at: null,
    copied_from_conversation_id: null, copied_through_message_id: null, copied_at: null,
    created_at: now, updated_at: now, deleted_at: null, messages: [],
  };
  const commands = {
    get_app_settings: {
      schema_version: 7,
      general: { interface_language: "en" },
      content: { remote_image_policy: "ask" },
      web_search: { enabled: false, privacy_acknowledged: false, default_backend_id: null,
        safe_search: true, default_language: "auto", default_region: "auto", backends: [] },
      sidebar: {
        standard_chats_collapsed: false, projects_collapsed: false,
        collapsed_chat_group_ids: [], collapsed_project_ids: [], width_px: 280,
      },
      models: {
        providers: [{
          id: "builtin-demo", name: "Built-in Demo", kind: "fake", preset_id: "builtin_demo",
          entered_base_url: "memory://fake", resolved_api_base_url: "memory://fake",
          endpoint_mode: "preset", enabled: true, credential_ref: null,
          models: [{ id: "demo", source: "discovered", status: "available", enabled: true,
            capabilities: { text: "yes", vision: "unknown", reasoning: "unknown", tools: "unknown",
              context_length: null, max_output_tokens: null, context_length_source: "fallback",
              input_price_per_million: null, output_price_per_million: null },
            last_seen_at: null }], last_discovered_at: null,
        }],
        default_provider_profile_id: "builtin-demo", default_model_id: "demo",
        connect_timeout_secs: 15, first_output_timeout_secs: 60, chunk_timeout_secs: 30,
        total_timeout_secs: 600,
      },
    },
    app_update_status: { state: "idle", current_version: "0.14.1", channel: "preview", update_id: null, version: null, notes: null, downloaded_bytes: 0, total_bytes: null, error_code: null },
    check_app_update: { state: "no_update", current_version: "0.14.1", channel: "preview", update_id: null, version: null, notes: null, downloaded_bytes: 0, total_bytes: null, error_code: null },
    download_app_update: { state: "error", current_version: "0.14.1", channel: "preview", update_id: null, version: null, notes: null, downloaded_bytes: 0, total_bytes: null, error_code: "not_ready" },
    install_app_update: null,
    load_update_draft: null,
    ack_update_draft: null,
    agent_core_status: { running: true, healthy: true, issue: null },
    ping_agent_core: { core_version: "0.14.1", protocol_version: 18, schema_version: 25, build_id: "schema25-local-runtime-v1" },
    list_skills: [],
    get_global_instructions: { global_instructions: { content: "", enabled: true, revision: 1 } },
    list_projects: [project],
    list_chat_groups: [],
    list_conversations: [work],
    get_conversation: work,
    get_context_compaction_status: { compaction: null },
    resume_context_compaction: { compaction: null },
    pause_context_compaction: { compaction: null },
    set_attachment_drop_target: null,
    "plugin:event|listen": 1,
    "plugin:event|unlisten": null,
    get_project_grant: null,
    get_project_model_scope: { project_id: project.id, policy: "inherit_global", models: [] },
    list_project_knowledge: { knowledge: [] },
    get_conversation_model_run: { run: null },
    list_trash: [],
    list_conversation_tool_operations: overrides.list_tool_operations ?? { operations: [] },
    update_sidebar_width: {
      schema_version: 3,
      general: { interface_language: "en" },
      sidebar: {
        standard_chats_collapsed: false, projects_collapsed: false,
        collapsed_chat_group_ids: [], collapsed_project_ids: [], width_px: 360,
      },
    },
    update_remote_image_policy: {
      schema_version: 7,
      general: { interface_language: "en" },
      content: { remote_image_policy: "allow" },
      sidebar: {
        standard_chats_collapsed: false, projects_collapsed: false,
        collapsed_chat_group_ids: [], collapsed_project_ids: [], width_px: 280,
      },
      models: {
        providers: [], default_provider_profile_id: "builtin-demo", default_model_id: "demo",
        connect_timeout_secs: 15, first_output_timeout_secs: 60, chunk_timeout_secs: 30,
        total_timeout_secs: 600,
      },
    },
    ...overrides,
  };

  await page.addInitScript((responses) => {
    let nextCallback = 1;
    window.__TAURI_INTERNALS__ = {
      metadata: { currentWindow: { label: "main" }, currentWebview: { label: "main" } },
      invoke: async (command, args) => {
        if (!(command in responses)) throw new Error(`Unexpected Tauri command: ${command}`);
        if (command === "update_sidebar_width") {
          responses.get_app_settings.sidebar.width_px = args.widthPx;
          return structuredClone(responses.get_app_settings);
        }
        return responses[command];
      },
      transformCallback: (callback, once) => {
        const id = nextCallback++;
        window[`_${id}`] = (payload) => {
          if (once) delete window[`_${id}`];
          callback?.(payload);
        };
        return id;
      },
      unregisterCallback: (id) => { delete window[`_${id}`]; },
      convertFileSrc: (path) => path,
    };
    window.__TAURI_EVENT_PLUGIN_INTERNALS__ = {
      unregisterListener: () => undefined,
    };
  }, commands);

  return { project, work, now };
}
