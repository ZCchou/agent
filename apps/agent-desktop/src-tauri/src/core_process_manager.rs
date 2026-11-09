use crate::logging::{events as log_events, AppLogger, LogLevel};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
};
use tauri::async_runtime::{channel, Receiver, Sender};
use tauri::{AppHandle, Emitter, Manager};

use tauri_plugin_shell::{process::CommandEvent, ShellExt};

#[cfg(windows)]
use crate::core_transport::CoreTransport as CommandChild;
#[cfg(not(windows))]
use tauri_plugin_shell::process::CommandChild;

fn spawn_core(
    command: tauri_plugin_shell::process::Command,
) -> Result<(Receiver<CommandEvent>, CommandChild), String> {
    #[cfg(windows)]
    {
        CommandChild::spawn(command.into())
    }
    #[cfg(not(windows))]
    {
        command.spawn().map_err(|error| error.to_string())
    }
}

/// 把一条 JSON 消息转换成 NDJSON 的一行。
///
/// agent 的 Rust ↔ Python IPC 使用 NDJSON：
/// 每一行代表一条完整 JSON 消息，因此结尾必须恰好有一个换行符。
fn build_ndjson_line(message: &str) -> String {
    let message = message.trim_end_matches(['\r', '\n']);

    format!("{message}\n")
}

/// Incrementally decodes stdout as NDJSON.
///
/// The shell plugin normally emits one line per event, but this is not part of
/// the IPC contract: an OS read may split one JSON object or combine several
/// lines. Keeping the remainder here prevents a harmless pipe boundary from
/// looking like a Core protocol failure.
#[derive(Default)]
struct NdjsonDecoder {
    buffered: Vec<u8>,
}

impl NdjsonDecoder {
    fn push(&mut self, chunk: &[u8]) -> Vec<Result<Value, serde_json::Error>> {
        self.buffered.extend_from_slice(chunk);
        let mut decoded = Vec::new();

        while let Some(newline) = self.buffered.iter().position(|byte| *byte == b'\n') {
            let mut line = self.buffered.drain(..=newline).collect::<Vec<_>>();
            while matches!(line.last(), Some(b'\n' | b'\r')) {
                line.pop();
            }
            if !line.is_empty() {
                decoded.push(serde_json::from_slice(&line));
            }
        }

        // Some versions of the shell plugin remove the line terminator before
        // delivering stdout. Accept an already complete JSON value while still
        // retaining genuinely partial objects for the next event.
        if !self.buffered.is_empty() {
            if let Ok(value) = serde_json::from_slice::<Value>(&self.buffered) {
                self.buffered.clear();
                decoded.push(Ok(value));
            }
        }

        decoded
    }
}

fn accept_run_event(sequences: &mut HashMap<String, u64>, response: &Value) -> bool {
    if response.get("v").and_then(Value::as_u64) != Some(crate::core_bridge::PROTOCOL_VERSION) {
        return false;
    }
    let Some(event) = response.get("event") else {
        return false;
    };
    let Some(run_id) = event.get("run_id").and_then(Value::as_str) else {
        return false;
    };
    if run_id.is_empty() {
        return false;
    }
    let Some(sequence) = event.get("sequence").and_then(Value::as_u64) else {
        return false;
    };
    if sequence == 0 {
        return false;
    }
    let Some(event_type) = event.get("event_type").and_then(Value::as_str) else {
        return false;
    };
    if !matches!(
        event_type,
        "run.state_changed"
            | "run.chunk"
            | "run.draft_checkpoint"
            | "run.completed"
            | "run.failed"
            | "run.cancelled"
            | "run.tool_requested"
            | "run.tool_status_changed"
            | "run.change_set_ready"
            | "run.waiting_approval"
            | "run.tool_completed"
            | "run.web_search_started"
            | "run.web_search_completed"
            | "run.web_search_failed"
            | "run.context_compaction_started"
            | "run.context_compaction_completed"
            | "run.context_compaction_failed"
            | "run.waiting_context"
            | "run.planning_started"
            | "run.plan_ready"
            | "run.waiting_permission"
            | "run.waiting_budget"
            | "run.step_started"
            | "run.step_completed"
            | "run.step_failed"
            | "run.synthesis_started"
            | "run.invocation_prepared"
            | "run.invocation_dispatched"
            | "run.invocation_retrying"
            | "run.capabilities_requested"
            | "run.response_recovering"
            | "run.invocation_completed"
            | "run.invocation_failed"
    ) {
        return false;
    }
    if event.get("timestamp").and_then(Value::as_str).is_none()
        || event.get("data").and_then(Value::as_object).is_none()
    {
        return false;
    }
    let previous = sequences.get(run_id).copied().unwrap_or(0);
    if sequence <= previous {
        return false;
    }
    sequences.insert(run_id.to_string(), sequence);
    true
}

fn terminal_event_run_id(response: &Value) -> Option<&str> {
    let event = response.get("event")?;
    let event_type = event.get("event_type")?.as_str()?;
    matches!(
        event_type,
        "run.completed"
            | "run.failed"
            | "run.cancelled"
            | "run.context_compaction_completed"
            | "run.context_compaction_failed"
    )
    .then(|| event.get("run_id")?.as_str())
    .flatten()
}

/// 管理 agent 的 Python Agent Core 子进程。
///
/// 职责：
/// 1. 启动 Python Core；
/// 2. 保存子进程句柄；
/// 3. 持续监听 stdout / stderr；
/// 4. 维护 Core 的运行状态；
/// 5. 防止重复启动；
/// 6. 主动关闭 Core。
pub struct CoreProcessManager {
    /// Python Core 的进程控制句柄。
    ///
    /// 主要用于后续向 stdin 写消息以及关闭进程。
    child: Option<CommandChild>,

    /// Python Core 当前是否处于运行状态。
    ///
    /// 使用 Arc<AtomicBool> 是因为：
    /// Manager 和后台事件监听任务都需要访问这个状态。
    running: Arc<AtomicBool>,

    /// 最近一次经过完整协议校验的 ping 是否成功。
    healthy: Arc<AtomicBool>,

    /// 当前已经发送、但还没有收到 Python 响应的请求。
    ///
    /// key:
    ///     request_id
    ///
    /// value:
    ///     Python 返回结果后，用这个 Sender
    ///     把响应交给正在等待该 request_id 的调用者。
    pending: Arc<Mutex<HashMap<String, Sender<Value>>>>,
}

// An owned identity cannot be reused while an old request still retains it.
pub(crate) struct RequestOwner(Arc<AtomicBool>);

impl CoreProcessManager {
    /// 创建一个尚未启动 Python Core 的 Manager。
    pub fn new() -> Self {
        Self {
            child: None,

            running: Arc::new(AtomicBool::new(false)),
            healthy: Arc::new(AtomicBool::new(false)),

            pending: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub(crate) fn request_owner(&self) -> RequestOwner {
        RequestOwner(self.running.clone())
    }

    pub(crate) fn owns_request(&self, owner: &RequestOwner) -> bool {
        Arc::ptr_eq(&self.running, &owner.0)
    }

    pub(crate) fn reset_process_state(&mut self) {
        // Old reader cleanup retains only the old process's flags and request table.
        if let Ok(mut pending) = self.pending.lock() {
            pending.clear();
        }
        self.running = Arc::new(AtomicBool::new(true));
        self.healthy = Arc::new(AtomicBool::new(false));
        self.pending = Arc::new(Mutex::new(HashMap::new()));
    }

    /// 返回最近观测到的 Python Core 运行状态。
    pub fn is_running(&self) -> bool {
        self.running.load(Ordering::SeqCst)
    }

    pub fn is_healthy(&self) -> bool {
        self.is_running() && self.healthy.load(Ordering::SeqCst)
    }

    pub fn set_healthy(&self, healthy: bool) {
        self.healthy.store(healthy, Ordering::SeqCst);
    }

    /// 检查是否允许启动新的 Agent Core。
    pub fn ensure_can_start(&self) -> Result<(), String> {
        if self.is_running() {
            return Err("Agent Core is already running".to_string());
        }

        Ok(())
    }

    /// 启动长期运行的 Python Agent Core。
    pub fn start(&mut self, app: &AppHandle) -> Result<(), String> {
        self.ensure_can_start()?;

        // 全局 AppLogger 已经在 lib.rs 初始化完成。
        //
        // CoreProcessManager 不再自行解析操作系统日志目录，
        // 而是统一复用 AppLogger 持有的目录。
        let logger = app.state::<AppLogger>();

        // =========================================================
        // 1. 记录 Core 启动请求
        // =========================================================

        logger.host(
            LogLevel::Info,
            log_events::CORE_START_REQUESTED,
            "Agent Core start requested",
            json!({}),
        );

        // =========================================================
        // 2. 创建 Python Agent Core sidecar Command
        // =========================================================

        let mut command = match app.shell().sidecar("agent-core") {
            Ok(command) => command,

            Err(error) => {
                logger.host(
                    LogLevel::Error,
                    log_events::CORE_START_FAILED,
                    "Failed to create Agent Core sidecar",
                    json!({
                        "error_type":
                            "sidecar_creation_failed"
                    }),
                );

                return Err(format!("Failed to create Agent Core sidecar: {error}"));
            }
        };

        // =========================================================
        // 3. 配置 Python Core 日志环境
        // =========================================================
        //
        // Rust Host 是平台路径信息的唯一来源。
        //
        // Python 不需要知道：
        //
        // - Windows 日志目录在哪里
        // - macOS 日志目录在哪里
        // - Linux 日志目录在哪里
        //
        // Python 只读取环境变量。

        let log_level = if cfg!(debug_assertions) {
            "DEBUG"
        } else {
            "INFO"
        };

        command = command.env("AGENT_LOG_LEVEL", log_level);

        let data_dir = app
            .path()
            .app_data_dir()
            .map_err(|error| format!("Failed to resolve application data directory: {error}"))?;
        command = command.env("AGENT_DATA_DIR", data_dir.as_os_str());

        // AppLogger 初始化成功并拥有文件日志目录时，
        // 将同一个目录传给 Python Core。
        //
        // 最终目录中会同时出现：
        //
        // host-YYYY-MM-DD.jsonl
        // ipc-YYYY-MM-DD.jsonl
        // core-YYYY-MM-DD.jsonl
        if let Some(log_dir) = logger.log_dir() {
            command = command.env("AGENT_LOG_DIR", log_dir.as_os_str());
        }

        // =========================================================
        // 4. 启动 Python Core
        // =========================================================

        let (mut events, child) = match spawn_core(command) {
            Ok(result) => result,

            Err(error) => {
                logger.host(
                    LogLevel::Error,
                    log_events::CORE_START_FAILED,
                    "Failed to spawn Agent Core",
                    json!({
                        "error_type":
                            "sidecar_spawn_failed"
                    }),
                );

                return Err(format!("Failed to start Agent Core: {error}"));
            }
        };

        // 保存 PID，之后即使 child 被移动或进程退出，
        // 日志仍然可以标识具体是哪一个 Python Core。
        let child_pid = child.pid();

        // =========================================================
        // 5. 更新运行状态
        // =========================================================
        //
        // 必须在后台监听任务启动前写 true。
        //
        // 防止 Python 启动后立即退出时产生竞态：
        //
        // listener 写 false
        // ↓
        // start() 又错误写回 true

        self.reset_process_state();
        let search_generation = app
            .state::<crate::file_search::FileSearchService>()
            .begin_core_generation();

        logger.host(
            LogLevel::Info,
            log_events::CORE_STARTED,
            "Agent Core started",
            json!({
                "pid":
                    child_pid,

                "log_level":
                    log_level,

                "file_logging":
                    logger.log_dir().is_some()
            }),
        );

        // =========================================================
        // 6. 给后台事件监听器准备共享状态
        // =========================================================

        let running = Arc::clone(&self.running);
        let healthy = Arc::clone(&self.healthy);

        let pending = Arc::clone(&self.pending);

        // State<AppLogger> 本身不能直接长期借给异步任务。
        //
        // 所以这里 clone AppHandle，
        // 后台任务需要写日志时，
        // 再通过 app_for_events.state::<AppLogger>()
        // 获取全局 Logger。
        let app_for_events = app.clone();

        // =========================================================
        // 7. 后台监听 Python Core
        // =========================================================

        tauri::async_runtime::spawn(async move {
            let mut event_sequences: HashMap<String, u64> = HashMap::new();
            let mut stdout_decoder = NdjsonDecoder::default();
            while let Some(event) = events.recv().await {
                if !running.load(Ordering::SeqCst) {
                    break;
                }
                match event {
                    // =========================================
                    // Python stdout
                    //
                    // stdout 是 NDJSON IPC 专用通道。
                    // =========================================
                    CommandEvent::Stdout(bytes) => {
                        for decoded in stdout_decoder.push(&bytes) {
                            if !running.load(Ordering::SeqCst) {
                                break;
                            }
                            let response: Value = match decoded {
                                Ok(response) => response,

                                Err(error) => {
                                    let logger = app_for_events.state::<AppLogger>();

                                    logger.ipc(
                                        LogLevel::Warn,
                                        log_events::IPC_INVALID_RESPONSE,
                                        "Invalid JSON response from Agent Core",
                                        json!({
                                            "error_type":
                                                "invalid_json",

                                            "parser_error":
                                                error.to_string()
                                        }),
                                    );

                                    continue;
                                }
                            };

                            if let Some(event) = response.get("event") {
                                if !accept_run_event(&mut event_sequences, &response) {
                                    continue;
                                }
                                let _ = app_for_events.emit("model-run-event", event.clone());
                                crate::tool_event_handler::dispatch(
                                    app_for_events.clone(),
                                    event.clone(),
                                    search_generation,
                                );
                                if let Some(run_id) = terminal_event_run_id(&response) {
                                    event_sequences.remove(run_id);
                                }
                                continue;
                            }

                            // 所有需要匹配请求的响应
                            // 都必须携带 request id。
                            let request_id = match response.get("id").and_then(Value::as_str) {
                                Some(id) => id.to_string(),

                                None => {
                                    let logger = app_for_events.state::<AppLogger>();

                                    logger.ipc(
                                        LogLevel::Warn,
                                        log_events::IPC_INVALID_RESPONSE,
                                        "Agent Core response is missing request id",
                                        json!({
                                            "error_type":
                                                "missing_request_id"
                                        }),
                                    );

                                    continue;
                                }
                            };

                            // =====================================
                            // 根据 request_id 找等待者
                            // =====================================
                            //
                            // remove() 很重要。
                            //
                            // 一条请求收到最终响应以后，
                            // 就不能继续留在 pending 表里。

                            let sender = {
                                match pending.lock() {
                                    Ok(mut pending) => pending.remove(&request_id),

                                    Err(_) => {
                                        let logger = app_for_events.state::<AppLogger>();

                                        logger.ipc(
                                            LogLevel::Error,
                                            log_events::IPC_REQUEST_FAILED,
                                            "Failed to lock pending request table",
                                            json!({
                                                "request_id":
                                                    request_id
                                            }),
                                        );

                                        None
                                    }
                                }
                            };

                            match sender {
                                Some(sender) => {
                                    // 把 Python 响应交给真正等待
                                    // 这个 request_id 的 Rust 调用者。
                                    if sender.send(response).await.is_err() {
                                        let logger = app_for_events.state::<AppLogger>();

                                        logger.ipc(
                                            LogLevel::Warn,
                                            log_events::IPC_REQUEST_FAILED,
                                            "IPC request receiver was dropped",
                                            json!({
                                                "request_id":
                                                    request_id
                                            }),
                                        );
                                    }
                                }

                                None => {
                                    // 收到一个当前没有任何调用者
                                    // 等待的 response id。
                                    //
                                    // 可能原因包括：
                                    //
                                    // - 请求已经超时
                                    // - Receiver 已释放
                                    // - Python 返回错误 id
                                    let logger = app_for_events.state::<AppLogger>();

                                    logger.ipc(
                                        LogLevel::Warn,
                                        log_events::IPC_RESPONSE_UNMATCHED,
                                        "No pending request matches Agent Core response",
                                        json!({
                                            "request_id":
                                                request_id
                                        }),
                                    );
                                }
                            }
                        }
                    }

                    // =========================================
                    // Python stderr
                    // =========================================
                    //
                    // Python 自己的 ConsoleFormatter 本来就
                    // 输出到 stderr。
                    //
                    // 这里继续把它转发到开发终端即可。
                    //
                    // 不应该再完整复制进 host.jsonl，
                    // 否则同一条 Python 日志会重复两份。
                    CommandEvent::Stderr(bytes) => {
                        let text = String::from_utf8_lossy(&bytes);

                        eprintln!("[Agent Core stderr] {}", text.trim_end());
                    }

                    // =========================================
                    // Sidecar 事件流错误
                    // =========================================
                    CommandEvent::Error(error) => {
                        let logger = app_for_events.state::<AppLogger>();

                        logger.host(
                            LogLevel::Error,
                            log_events::CORE_TERMINATED,
                            "Agent Core process stream error",
                            json!({
                                "pid":
                                    child_pid,

                                "error_type":
                                    "process_stream_error",

                                "error":
                                    error.to_string()
                            }),
                        );
                    }

                    // =========================================
                    // Python Core 真正退出
                    // =========================================
                    CommandEvent::Terminated(payload) => {
                        app_for_events
                            .state::<crate::file_search::FileSearchService>()
                            .end_core_generation(search_generation);
                        running.store(false, Ordering::SeqCst);
                        healthy.store(false, Ordering::SeqCst);

                        // Python 已经退出。
                        //
                        // 所有尚未完成的 pending 请求
                        // 都不可能再收到响应。
                        //
                        // 清空 Sender 后，
                        // 对应 Receiver 会感知通道关闭，
                        // 避免永久等待。
                        if let Ok(mut pending) = pending.lock() {
                            pending.clear();
                        }

                        let logger = app_for_events.state::<AppLogger>();

                        logger.host(
                            LogLevel::Info,
                            log_events::CORE_TERMINATED,
                            "Agent Core process terminated",
                            json!({
                                "pid":
                                    child_pid,

                                "exit_code":
                                    payload.code,

                                "signal":
                                    payload.signal
                            }),
                        );

                        break;
                    }

                    // CommandEvent 是非穷举类型，
                    // 保留兜底分支。
                    _ => {}
                }
            }

            // =============================================
            // 防御性状态同步
            // =============================================
            //
            // 即使事件 channel 异常关闭，
            // 没有收到 Terminated，
            // Manager 也不能继续认为 Python 正在运行。

            app_for_events
                .state::<crate::file_search::FileSearchService>()
                .end_core_generation(search_generation);
            running.store(false, Ordering::SeqCst);
            healthy.store(false, Ordering::SeqCst);

            if let Ok(mut pending) = pending.lock() {
                pending.clear();
            }
        });

        // =========================================================
        // 8. Manager 持有 Python 进程句柄
        // =========================================================

        self.child = Some(child);

        Ok(())
    }
    /// 向已经运行的 Python Agent Core 发送一条 NDJSON 消息。
    ///
    /// 这里不会创建新的 Python 进程。
    /// 所有消息都会通过当前保存的 `child` 写入同一个 Core 的 stdin。
    fn send(&mut self, request_id: &str, message: &str) -> Result<(), String> {
        // 没启动 Core 时禁止发送。
        if !self.is_running() {
            return Err("Agent Core is not running".to_string());
        }

        // running=true 理论上意味着 child 必须存在。
        // 如果不存在，说明 Manager 内部状态已经不一致。
        let child = self
            .child
            .as_mut()
            .ok_or_else(|| "Agent Core is running but child handle is missing".to_string())?;

        let line = build_ndjson_line(message);

        // CommandChild::write() 实际写入 Python 进程的 stdin。
        #[cfg(windows)]
        child.write(request_id, line.as_bytes())?;
        #[cfg(not(windows))]
        {
            let _ = request_id;
            child
                .write(line.as_bytes())
                .map_err(|error| format!("Failed to write to Agent Core: {error}"))?;
        }

        Ok(())
    }
    /// 发送一条需要等待响应的 IPC 请求。
    ///
    /// 工作流程：
    /// 1. 为当前 request_id 创建一个容量为 1 的异步通道；
    /// 2. 把 Sender 保存到 pending；
    /// 3. 把 JSON 请求写入 Python stdin；
    /// 4. 把 Receiver 返回给调用方。
    ///
    /// Python 返回响应后，后台 stdout listener 会根据 request_id
    /// 找到对应 Sender，再把结果发送给这个 Receiver。
    pub fn send_request(
        &mut self,
        request_id: &str,
        message: &str,
    ) -> Result<Receiver<Value>, String> {
        if !self.is_running() {
            return Err("Agent Core is not running".to_string());
        }

        // 一个请求只期待一条最终响应，因此容量 1 已经足够。
        let (sender, receiver) = channel::<Value>(1);

        {
            let mut pending = self
                .pending
                .lock()
                .map_err(|_| "Failed to lock pending request table".to_string())?;

            // 理论上 UUID 几乎不会重复，但这里仍然做防御性检查。
            if pending.contains_key(request_id) {
                return Err(format!("Duplicate Agent Core request id: {request_id}"));
            }

            pending.insert(request_id.to_string(), sender);
        }

        // 真正写入 Python stdin。
        //
        // 如果发送失败，必须把刚才登记的 pending 清掉，
        // 否则会留下永远等不到结果的“幽灵请求”。
        if let Err(error) = self.send(request_id, message) {
            if let Ok(mut pending) = self.pending.lock() {
                pending.remove(request_id);
            }

            return Err(error);
        }

        Ok(receiver)
    }

    pub fn cancel_request(&self, request_id: &str) {
        #[cfg(windows)]
        if let Some(child) = &self.child {
            child.cancel_request(request_id);
        }
        if let Ok(mut pending) = self.pending.lock() {
            pending.remove(request_id);
        }
    }
    /// 主动关闭 Python Agent Core。
    pub fn shutdown_for_update(&mut self) -> Result<(), String> {
        if !self.is_running() {
            self.child = None;
            return Ok(());
        }
        #[cfg(windows)]
        if let Some(child) = &self.child {
            child.close_stdin_and_wait(8000)?;
        }
        #[cfg(not(windows))]
        return Err("app_update_core_unavailable: Graceful update shutdown is not supported on this platform.".into());
        #[cfg(windows)]
        {
            self.child = None;
            self.running.store(false, Ordering::SeqCst);
            self.healthy.store(false, Ordering::SeqCst);
            Ok(())
        }
    }

    /// 主动关闭 Python Agent Core。
    pub fn shutdown(&mut self) -> Result<(), String> {
        // 如果 Core 已经因为崩溃等原因退出，
        // 清理残留的旧 child 句柄即可。
        if !self.is_running() {
            self.child = None;
            return Ok(());
        }

        let child = self
            .child
            .take()
            .ok_or_else(|| "Agent Core is running but child handle is missing".to_string())?;

        // kill() 会消费 CommandChild，
        // 所以我们前面先通过 take() 把它从 Option 中取出。
        if let Err(error) = child.kill() {
            // The handle is consumed by kill(). Avoid reporting a healthy
            // process that the host can no longer control.
            self.running.store(false, Ordering::SeqCst);
            self.healthy.store(false, Ordering::SeqCst);
            return Err(format!("Failed to stop Agent Core: {error}"));
        }

        // 不需要等 Terminated 事件才能更新 UI 状态。
        // Terminated 到来时后台任务还会再次写 false，
        // 重复写 false 是安全的。
        self.running.store(false, Ordering::SeqCst);
        self.healthy.store(false, Ordering::SeqCst);

        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::Ordering;

    use super::{
        accept_run_event, build_ndjson_line, terminal_event_run_id, CoreProcessManager,
        NdjsonDecoder,
    };
    use serde_json::json;
    use std::collections::HashMap;
    #[test]
    fn builds_ndjson_line_with_single_newline() {
        let message = r#"{"type":"ping"}"#;

        let line = build_ndjson_line(message);

        assert_eq!(line, "{\"type\":\"ping\"}\n");
    }
    #[test]
    fn does_not_duplicate_ndjson_newline() {
        let message = "{\"type\":\"ping\"}\n";

        let line = build_ndjson_line(message);

        assert_eq!(line, "{\"type\":\"ping\"}\n");
    }

    #[test]
    fn ndjson_decoder_accepts_stdout_without_line_terminator() {
        let mut decoder = NdjsonDecoder::default();

        let values = decoder.push(br#"{"v":5,"id":"one","result":{}}"#);

        assert_eq!(values.len(), 1);
        assert_eq!(values[0].as_ref().unwrap()["id"], "one");
    }

    #[test]
    fn ndjson_decoder_reassembles_fragmented_json() {
        let mut decoder = NdjsonDecoder::default();

        assert!(decoder.push(b"{\"v\":5,\"id\":\"").is_empty());
        let values = decoder.push(b"one\",\"result\":{}}\n");

        assert_eq!(values.len(), 1);
        assert_eq!(values[0].as_ref().unwrap()["id"], "one");
    }

    #[test]
    fn ndjson_decoder_splits_combined_lines() {
        let mut decoder = NdjsonDecoder::default();

        let values = decoder.push(
            b"{\"v\":5,\"id\":\"one\",\"result\":{}}\n{\"v\":5,\"id\":\"two\",\"result\":{}}\n",
        );

        assert_eq!(values.len(), 2);
        assert_eq!(values[0].as_ref().unwrap()["id"], "one");
        assert_eq!(values[1].as_ref().unwrap()["id"], "two");
    }
    #[test]
    fn new_manager_is_not_running() {
        let manager = CoreProcessManager::new();

        assert!(!manager.is_running());
    }

    #[test]
    fn manager_allows_start_when_not_running() {
        let manager = CoreProcessManager::new();

        let result = manager.ensure_can_start();

        assert!(result.is_ok());
    }

    #[test]
    fn manager_rejects_start_when_running() {
        let manager = CoreProcessManager::new();

        // 单元测试不真的启动 Python，
        // 这里只模拟 Manager 已经处于 running 状态。
        manager.running.store(true, Ordering::SeqCst);

        let result = manager.ensure_can_start();

        assert!(result.is_err());
    }

    #[test]
    fn old_process_cleanup_cannot_change_new_process_flags_or_pending_receivers() {
        let mut manager = CoreProcessManager::new();
        let old_running = manager.running.clone();
        let old_healthy = manager.healthy.clone();
        let old_pending = manager.pending.clone();
        let (old_sender, _old_receiver) = tauri::async_runtime::channel(1);
        old_pending
            .lock()
            .unwrap()
            .insert("same-id".into(), old_sender);
        manager.reset_process_state();
        let (new_sender, _new_receiver) = tauri::async_runtime::channel(1);
        manager
            .pending
            .lock()
            .unwrap()
            .insert("same-id".into(), new_sender);
        manager.set_healthy(true);
        old_running.store(false, Ordering::SeqCst);
        old_healthy.store(false, Ordering::SeqCst);
        old_pending.lock().unwrap().clear();
        assert!(manager.is_running() && manager.is_healthy());
        assert!(manager.pending.lock().unwrap().contains_key("same-id"));
        assert!(!old_pending.lock().unwrap().contains_key("same-id"));
    }

    #[test]
    fn cancelling_one_pending_request_preserves_other_requests() {
        let manager = CoreProcessManager::new();
        let (first, _first_receiver) = tauri::async_runtime::channel(1);
        let (second, _second_receiver) = tauri::async_runtime::channel(1);
        {
            let mut pending = manager.pending.lock().unwrap();
            pending.insert("first".to_string(), first);
            pending.insert("second".to_string(), second);
        }
        manager.cancel_request("first");
        let pending = manager.pending.lock().unwrap();
        assert!(!pending.contains_key("first"));
        assert!(pending.contains_key("second"));
    }

    #[test]
    fn run_events_are_validated_and_deduplicated_per_run() {
        let mut sequences = HashMap::new();
        assert!(accept_run_event(
            &mut sequences,
            &json!({"v":19,"event":{
            "run_id":"r1","sequence":1,"event_type":"run.chunk","timestamp":"now","data":{}}})
        ));
        assert!(!accept_run_event(
            &mut sequences,
            &json!({"v":19,"event":{
            "run_id":"r1","sequence":1,"event_type":"run.chunk","timestamp":"now","data":{}}})
        ));
        assert!(!accept_run_event(
            &mut sequences,
            &json!({"v":1,"event":{
            "run_id":"r1","sequence":2}})
        ));
        assert!(accept_run_event(
            &mut sequences,
            &json!({"v":19,"event":{
            "run_id":"r2","sequence":1,"event_type":"run.failed","timestamp":"now","data":{}}})
        ));
        assert!(!accept_run_event(
            &mut sequences,
            &json!({"v":10,"event":{
            "run_id":"r3","sequence":1,"event_type":"run.unknown","timestamp":"now","data":{}}})
        ));
        assert!(accept_run_event(
            &mut sequences,
            &json!({"v":19,"event":{
            "run_id":"r4","sequence":1,"event_type":"run.invocation_dispatched",
            "timestamp":"now","data":{"spec_id":"spec-1"}}})
        ));
    }

    #[test]
    fn runtime_capability_and_recovery_events_reach_the_host_once() {
        let mut sequences = HashMap::new();
        for (index, event_type) in ["run.capabilities_requested", "run.response_recovering"]
            .iter()
            .enumerate()
        {
            let response = json!({"v":19,"event":{"run_id":"cap-run","sequence":index+1,
                "event_type":event_type,"timestamp":"now","data":{"request_id":"observation"}}});
            assert!(accept_run_event(&mut sequences, &response));
            assert!(!accept_run_event(&mut sequences, &response));
        }
    }

    #[test]
    fn terminal_events_identify_sequence_state_to_release() {
        let response = json!({"v":19,"event":{
            "run_id":"r1","sequence":3,"event_type":"run.completed",
            "timestamp":"now","data":{}
        }});

        assert_eq!(terminal_event_run_id(&response), Some("r1"));
        assert_eq!(
            terminal_event_run_id(&json!({"v":19,"event":{
                "run_id":"r1","sequence":2,"event_type":"run.chunk",
                "timestamp":"now","data":{}
            }})),
            None
        );
    }
}
