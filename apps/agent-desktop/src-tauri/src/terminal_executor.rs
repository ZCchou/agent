//! Finite Windows commands. cwd authority is pinned; command code is full-user execution.
use crate::file_access::{AuthorizedRoot, FileAccessPolicy, VerifiedReadHandle};
use crate::file_search::Cancellation;
use base64::Engine;
use serde::{Deserialize, Serialize};
use std::{
    io::{Read, Write},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};

pub(crate) const MAX_TIMEOUT_MS: u64 = 600000;
pub(crate) const MAX_OUTPUT_BYTES: usize = 1048576;

#[cfg(windows)]
pub(crate) fn powershell_path() -> Result<std::path::PathBuf, String> {
    let mut directory = [0u16; 32768];
    let size = unsafe {
        windows_sys::Win32::System::SystemInformation::GetSystemDirectoryW(
            directory.as_mut_ptr(),
            directory.len() as u32,
        )
    } as usize;
    if size == 0 || size >= directory.len() {
        return Err("terminal_spawn_failed: Windows system directory unavailable".into());
    }
    use std::os::windows::ffi::OsStringExt;
    Ok(
        std::path::PathBuf::from(std::ffi::OsString::from_wide(&directory[..size]))
            .join("WindowsPowerShell/v1.0/powershell.exe"),
    )
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TerminalRequest {
    pub command: String,
    #[serde(default = "cwd_default")]
    pub cwd: String,
    #[serde(default = "timeout_default")]
    pub timeout_ms: u64,
    #[serde(default = "output_default")]
    pub max_output_bytes: usize,
}
fn cwd_default() -> String {
    ".".into()
}
fn timeout_default() -> u64 {
    120000
}
fn output_default() -> usize {
    65536
}
impl TerminalRequest {
    pub fn parse(value: &serde_json::Value) -> Result<Self, String> {
        let request: Self = serde_json::from_value(value.clone())
            .map_err(|_| "invalid_tool_call: Invalid terminal arguments")?;
        if request.command.trim().is_empty()
            || request.command.chars().count() > 32768
            || request.command.contains('\0')
            || request.cwd.is_empty()
            || request.cwd.chars().count() > 1024
            || request.cwd.contains('\0')
            || !(1000..=MAX_TIMEOUT_MS).contains(&request.timeout_ms)
            || !(1024..=MAX_OUTPUT_BYTES).contains(&request.max_output_bytes)
        {
            return Err("invalid_tool_call: Invalid terminal arguments".into());
        }
        Ok(request)
    }
}
#[derive(Debug, Serialize)]
pub struct TerminalResult {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: Option<i32>,
    pub duration_ms: u64,
    pub timed_out: bool,
    pub cancelled: bool,
    pub stdout_truncated: bool,
    pub stderr_truncated: bool,
}
type Reader = JoinHandle<std::io::Result<(String, bool)>>;
fn reader(
    mut pipe: impl Read + Send + 'static,
    budget: Arc<AtomicUsize>,
) -> Result<Reader, String> {
    std::thread::Builder::new()
        .name("terminal-output".into())
        .spawn(move || {
            let mut retained = Vec::new();
            let mut buffer = [0u8; 8192];
            let mut truncated = false;
            loop {
                let count = pipe.read(&mut buffer)?;
                if count == 0 {
                    break;
                }
                let previous = budget
                    .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |left| {
                        Some(left.saturating_sub(count))
                    })
                    .unwrap();
                let keep = count.min(previous);
                retained.extend_from_slice(&buffer[..keep]);
                truncated |= keep < count;
            }
            let maximum = retained.len();
            let mut text = String::from_utf8_lossy(&retained).into_owned();
            if text.len() > maximum {
                let mut end = maximum;
                while !text.is_char_boundary(end) {
                    end -= 1;
                }
                text.truncate(end);
                truncated = true;
            }
            Ok((text, truncated))
        })
        .map_err(|_| "terminal_capture_failed: Could not start output reader".into())
}

#[cfg(windows)]
mod platform {
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use windows_sys::Win32::System::JobObjects::*;
    pub struct Job(OwnedHandle);
    impl Job {
        pub fn new() -> Result<Self, String> {
            unsafe {
                let raw = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if raw.is_null() {
                    return Err("terminal_job_failed: Could not create process job".into());
                }
                let job = Self(OwnedHandle::from_raw_handle(raw));
                let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                if SetInformationJobObject(
                    job.0.as_raw_handle(),
                    JobObjectExtendedLimitInformation,
                    &limits as *const _ as *const _,
                    std::mem::size_of_val(&limits) as u32,
                ) == 0
                {
                    return Err("terminal_job_failed: Could not set process cleanup".into());
                }
                Ok(job)
            }
        }
        pub fn attach(&self, child: &std::process::Child) -> Result<(), String> {
            if unsafe { AssignProcessToJobObject(self.0.as_raw_handle(), child.as_raw_handle()) }
                == 0
            {
                Err("terminal_job_failed: Process was not admitted; no command executed".into())
            } else {
                Ok(())
            }
        }
    }
}
#[cfg(windows)]
use platform::Job;
#[cfg(not(windows))]
struct Job;

pub struct TerminalProcess {
    child: Child,
    job: Option<Job>,
    stdout: Option<Reader>,
    stderr: Option<Reader>,
    gate: Option<JoinHandle<std::io::Result<()>>>,
    _cwd: VerifiedReadHandle,
    started: Instant,
    timeout: Duration,
}
impl TerminalProcess {
    pub fn pin_cwd(
        root: &AuthorizedRoot,
        request: &TerminalRequest,
    ) -> Result<VerifiedReadHandle, String> {
        let directory =
            FileAccessPolicy.open_read(root, &FileAccessPolicy.validate_relative(&request.cwd)?)?;
        if !directory.is_dir() {
            return Err("not_a_directory: Terminal cwd must be a directory".into());
        }
        Ok(directory)
    }
    #[cfg(windows)]
    pub fn start(root: &AuthorizedRoot, request: &TerminalRequest) -> Result<Self, String> {
        use std::os::windows::process::CommandExt;
        let cwd = Self::pin_cwd(root, request)?;
        let job = Job::new()?;
        // Nothing supplied by the model is executed until the Host attaches the job.
        // The command travels through stdin after attachment, avoiding Windows command-line limits.
        // `$?` alone forgets nonterminating errors when a later statement succeeds.
        // Retain the shell's error records as well as the final native exit code.
        // EncodedCommand serializes stderr as CLIXML even with OutputFormat Text.
        // Only this trusted constant is passed via Command; user code remains behind stdin.
        // Suppress progress before New-Object can auto-load a module and emit startup progress.
        // End the formatter in finally: a user `exit` otherwise drops deferred table rows.
        let bootstrap = r#"
$ProgressPreference='SilentlyContinue';
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false);
$OutputEncoding=[Console]::OutputEncoding;
$line=[Console]::In.ReadLine();
if (!$line) { exit 125 };
$code=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line));
$global:LASTEXITCODE=0;
$global:Error.Clear();
try {
    $formatter={Out-Default}.GetSteppablePipeline();
    $formatter.Begin($true);
    try {
        & ([ScriptBlock]::Create($code)) | ForEach-Object { $formatter.Process($_) };
        $ok=$?;
    } finally { $formatter.End() };
    if ((!$ok -or $Error.Count -gt 0) -and $LASTEXITCODE -eq 0) { exit 1 };
    exit $LASTEXITCODE;
} catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }
"#;
        let shell = powershell_path()?;
        let child = Command::new(shell)
            .args([
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-OutputFormat",
                "Text",
                "-Command",
                bootstrap,
            ])
            .current_dir(cwd.path())
            .creation_flags(0x08000000)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| "terminal_spawn_failed: Could not start PowerShell")?;
        let mut process = Self {
            child,
            job: Some(job),
            stdout: None,
            stderr: None,
            gate: None,
            _cwd: cwd,
            started: Instant::now(),
            timeout: Duration::from_millis(request.timeout_ms),
        };
        process.job.as_ref().unwrap().attach(&process.child)?;
        let budget = Arc::new(AtomicUsize::new(request.max_output_bytes));
        process.stdout = Some(reader(
            process.child.stdout.take().unwrap(),
            budget.clone(),
        )?);
        process.stderr = Some(reader(process.child.stderr.take().unwrap(), budget)?);
        let payload = base64::engine::general_purpose::STANDARD.encode(request.command.as_bytes());
        let mut input = process.child.stdin.take().unwrap();
        // A dedicated writer lets timeout/cancellation cover a shell stalled before ReadLine.
        process.gate = Some(
            std::thread::Builder::new()
                .name("terminal-gate".into())
                .spawn(move || writeln!(input, "{payload}"))
                .map_err(|_| "terminal_spawn_failed: Could not release command gate")?,
        );
        Ok(process)
    }
    #[cfg(not(windows))]
    pub fn start(_root: &AuthorizedRoot, _request: &TerminalRequest) -> Result<Self, String> {
        Err("terminal_unavailable: PowerShell execution requires Windows".into())
    }
    pub fn wait(mut self, cancellation: Cancellation) -> Result<TerminalResult, String> {
        let mut cancelled = false;
        let mut timed_out = false;
        let exit_code = loop {
            if cancellation.check().is_err() {
                cancelled = true;
                break None;
            }
            if self.started.elapsed() >= self.timeout {
                timed_out = true;
                break None;
            }
            if let Some(status) = self
                .child
                .try_wait()
                .map_err(|_| "terminal_wait_failed: Process outcome unknown")?
            {
                break status.code();
            }
            std::thread::sleep(Duration::from_millis(20));
        };
        // Close the entire tree even after its parent exits; grandchildren may hold pipes open.
        self.job.take();
        if cancelled || timed_out {
            let _ = self.child.kill();
        }
        self.child
            .wait()
            .map_err(|_| "terminal_wait_failed: Process outcome unknown")?;
        if let Some(gate) = self.gate.take() {
            let delivered = gate
                .join()
                .map_err(|_| "terminal_spawn_failed: Command gate stopped")?;
            if !cancelled && !timed_out {
                delivered.map_err(|_| "terminal_spawn_failed: Command gate failed")?;
            }
        }
        let (stdout, stdout_truncated) = join(self.stdout.take())?;
        let (stderr, stderr_truncated) = join(self.stderr.take())?;
        Ok(TerminalResult {
            stdout,
            stderr,
            exit_code,
            duration_ms: self.started.elapsed().as_millis() as u64,
            timed_out,
            cancelled,
            stdout_truncated,
            stderr_truncated,
        })
    }
}
fn join(reader: Option<Reader>) -> Result<(String, bool), String> {
    reader
        .ok_or("terminal_capture_failed: Missing reader")?
        .join()
        .map_err(|_| "terminal_capture_failed: Reader stopped")?
        .map_err(|_| "terminal_capture_failed: Output could not be read".into())
}
impl Drop for TerminalProcess {
    fn drop(&mut self) {
        self.job.take();
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(gate) = self.gate.take() {
            let _ = gate.join();
        }
        if let Some(reader) = self.stdout.take() {
            let _ = reader.join();
        }
        if let Some(reader) = self.stderr.take() {
            let _ = reader.join();
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::{file_access::AuthorizedRoot, file_search::Cancellation};
    use serde_json::json;
    fn scope() -> (std::path::PathBuf, AuthorizedRoot) {
        let root = std::env::temp_dir().join(format!("agent-terminal-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let scope = AuthorizedRoot::for_host_path(
            &root.canonicalize().unwrap(),
            &std::path::PathBuf::new(),
        );
        (root, scope)
    }
    fn request(command: &str) -> TerminalRequest {
        TerminalRequest::parse(&json!({"command": command})).unwrap()
    }
    #[test]
    fn arguments_obey_contract() {
        assert!(TerminalRequest::parse(&json!({"command":"exit 0"})).is_ok());
        for value in [
            json!({"command":" "}),
            json!({"command":"x","timeout_ms":999}),
            json!({"command":"x","timeout_ms":true}),
            json!({"command":"x","cwd":""}),
            json!({"command":"x","max_output_bytes":1048577}),
            json!({"command":"x","other":1}),
        ] {
            assert!(TerminalRequest::parse(&value).is_err(), "{value}");
        }
    }
    #[test]
    fn real_project_script_stdout_stderr_and_native_nonzero() {
        let (root, scope) = scope();
        std::fs::write(root.join("test.ps1"), "if ((2+2) -ne 4) { exit 1 }; Write-Output 'test passed'; [Console]::Error.WriteLine('stderr marker'); cmd /c exit 7").unwrap();
        let result = TerminalProcess::start(&scope, &request("& ./test.ps1"))
            .unwrap()
            .wait(Cancellation::default())
            .unwrap();
        assert_eq!(result.exit_code, Some(7));
        assert!(result.stdout.contains("test passed"));
        assert!(result.stderr.contains("stderr marker"));
        assert!(!result.cancelled && !result.timed_out);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn shell_errors_cannot_be_hidden_by_later_successful_output() {
        let (root, scope) = scope();
        let result = TerminalProcess::start(
            &scope,
            &request("Write-Error 'shell failure'; Write-Output 'later success'"),
        )
        .unwrap()
        .wait(Cancellation::default())
        .unwrap();
        assert_eq!(result.exit_code, Some(1));
        assert!(result.stderr.contains("shell failure"));
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn powershell_objects_are_formatted_before_exit() {
        let (root, scope) = scope();
        std::fs::write(root.join("terminal-object-sentinel.txt"), "fixture").unwrap();
        for (command, expected_exit, native_marker) in [
            ("cmd /c echo native-before-table; Get-ChildItem -Force | Select-Object Name, Length", 0, "native-before-table"),
            ("Get-ChildItem -Force | Select-Object Name, Length; cmd /c echo native-after-table", 0, "native-after-table"),
            ("Get-ChildItem -Force | Select-Object Name, Length; exit 7", 7, ""),
            ("Get-ChildItem -Force | Select-Object Name, Length; cmd /c exit 9", 9, ""),
        ] {
            let result = TerminalProcess::start(&scope, &request(command))
                .unwrap()
                .wait(Cancellation::default())
                .unwrap();
            assert_eq!(result.exit_code, Some(expected_exit), "{command}: {result:?}");
            assert!(result.stdout.contains(native_marker), "{command}: {result:?}");
            assert!(result.stdout.contains("terminal-object-sentinel.txt"), "{command}: {result:?}");
            assert!(result.stdout.contains("Length"), "{command}: {result:?}");
            assert!(result.stderr.is_empty(), "{command}: {result:?}");
        }
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn powershell_progress_does_not_consume_text_capture_budget() {
        let (root, scope) = scope();
        let mut req = request("1..20 | ForEach-Object { Write-Progress -Activity ('progress-' + ('x' * 100)) -Status 'working' -PercentComplete $_ }; Write-Output 'output-after-progress'");
        req.max_output_bytes = 1024;
        let result = TerminalProcess::start(&scope, &req)
            .unwrap()
            .wait(Cancellation::default())
            .unwrap();
        assert_eq!(result.exit_code, Some(0), "{result:?}");
        assert!(
            result.stdout.contains("output-after-progress"),
            "{result:?}"
        );
        assert!(result.stderr.is_empty(), "{result:?}");
        assert!(
            !result.stdout_truncated && !result.stderr_truncated,
            "{result:?}"
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn powershell_explicit_exit_flushes_the_object_table() {
        let (root, scope) = scope();
        let result = TerminalProcess::start(
            &scope,
            &request("[pscustomobject]@{Name='explicit-exit-sentinel';Length=7}; exit 7"),
        )
        .unwrap()
        .wait(Cancellation::default())
        .unwrap();
        assert_eq!(result.exit_code, Some(7), "{result:?}");
        assert!(
            result.stdout.contains("explicit-exit-sentinel"),
            "{result:?}"
        );
        assert!(result.stdout.contains("Length"), "{result:?}");
        assert!(result.stderr.is_empty(), "{result:?}");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn powershell_unicode_errors_are_plain_text_and_keep_failure_after_success() {
        let (root, scope) = scope();
        let result = TerminalProcess::start(
            &scope,
            &request(
                "Write-Output '中文输出'; Write-Error '中文错误'; Write-Output 'later success'",
            ),
        )
        .unwrap()
        .wait(Cancellation::default())
        .unwrap();
        assert_eq!(result.exit_code, Some(1), "{result:?}");
        assert!(result.stdout.contains("中文输出"), "{result:?}");
        assert!(result.stdout.contains("later success"), "{result:?}");
        assert!(result.stderr.contains("中文错误"), "{result:?}");
        assert!(!result.stderr.contains("#< CLIXML"), "{result:?}");
        assert!(!result.stderr.contains("<Objs"), "{result:?}");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn flood_is_bounded_and_drained_without_deadlock() {
        let (root, scope) = scope();
        let mut req = request("1..3000 | ForEach-Object { [Console]::Out.WriteLine(('o'*100)); [Console]::Error.WriteLine(('e'*100)) }");
        req.max_output_bytes = 1024;
        let result = TerminalProcess::start(&scope, &req)
            .unwrap()
            .wait(Cancellation::default())
            .unwrap();
        assert_eq!(result.exit_code, Some(0));
        assert!(result.stdout.len() + result.stderr.len() <= 1024);
        assert!(result.stdout_truncated || result.stderr_truncated);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn timeout_kills_child_tree_before_delayed_sentinel() {
        let (root, scope) = scope();
        let script = "Start-Sleep -Seconds 3; Set-Content sentinel.txt escaped";
        let encoded = base64::Engine::encode(
            &base64::engine::general_purpose::STANDARD,
            script
                .encode_utf16()
                .flat_map(u16::to_le_bytes)
                .collect::<Vec<_>>(),
        );
        let mut req = request(&format!("Start-Process powershell.exe -WindowStyle Hidden -ArgumentList '-NoProfile','-EncodedCommand','{encoded}'; Start-Sleep -Seconds 15"));
        req.timeout_ms = 1000;
        let result = TerminalProcess::start(&scope, &req)
            .unwrap()
            .wait(Cancellation::default())
            .unwrap();
        assert!(result.timed_out);
        assert_eq!(result.exit_code, None);
        std::thread::sleep(std::time::Duration::from_secs(3));
        assert!(!root.join("sentinel.txt").exists());
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn cancellation_and_directory_escape_prevent_success() {
        let (root, scope) = scope();
        assert!(TerminalProcess::start(
            &scope,
            &TerminalRequest::parse(&json!({"command":"exit 0","cwd":"../outside"})).unwrap()
        )
        .is_err());
        let service = crate::file_search::FileSearchService::default();
        let task = service
            .admit_core_event(0, crate::file_search::CoreEventAction::Search("run"))
            .unwrap()
            .unwrap();
        let process = TerminalProcess::start(
            &scope,
            &request("Start-Sleep -Seconds 10; Set-Content sentinel.txt escaped"),
        )
        .unwrap();
        service.cancel_run("run");
        let result = process.wait(task.cancellation.clone()).unwrap();
        assert!(result.cancelled);
        assert_eq!(result.exit_code, None);
        assert!(!root.join("sentinel.txt").exists());
        std::fs::remove_dir_all(root).unwrap();
    }
}

#[cfg(test)]
mod runtime_integration {
    use super::*;
    use std::io::{BufRead, BufReader};
    #[test]
    fn synthetic_http_model_core_compiler_host_process_and_continuation() {
        let root =
            std::env::temp_dir().join(format!("agent-terminal-runtime-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        std::fs::write(
            root.join("test.ps1"),
            "Write-Output 'test passed'; cmd /c exit 7",
        )
        .unwrap();
        let scope = AuthorizedRoot::for_host_path(
            &root.canonicalize().unwrap(),
            &std::path::PathBuf::new(),
        );
        let core =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../services/agent-core");
        for kind in ["project_work", "standard_chat"] {
            let mut driver = Command::new(
                std::env::var("AGENT_TEST_PYTHON").unwrap_or_else(|_| "python".into()),
            )
            .arg("-X")
            .arg("utf8")
            .arg(core.join("tests/terminal_runtime_driver.py"))
            .arg(&root)
            .arg(kind)
            .env("PYTHONPATH", core.join("src"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
            let mut output = BufReader::new(driver.stdout.take().unwrap());
            let mut done = false;
            loop {
                let mut line = String::new();
                if output.read_line(&mut line).unwrap() == 0 {
                    break;
                }
                let value: serde_json::Value = serde_json::from_str(&line).unwrap();
                if let Some(operation) = value.get("operation") {
                    assert_eq!(kind, "project_work");
                    assert_eq!(operation["tool_name"], "terminal.run");
                    let request = TerminalRequest::parse(&operation["arguments"]).unwrap();
                    let result = TerminalProcess::start(&scope, &request)
                        .unwrap()
                        .wait(Cancellation::default())
                        .unwrap();
                    writeln!(
                        driver.stdin.as_mut().unwrap(),
                        "{}",
                        serde_json::to_string(&result).unwrap()
                    )
                    .unwrap();
                } else {
                    assert_eq!(value["done"], true);
                    done = true;
                }
            }
            let result = driver.wait_with_output().unwrap();
            assert!(
                result.status.success() && done,
                "{}",
                String::from_utf8_lossy(&result.stderr)
            );
        }
        std::fs::remove_dir_all(root).unwrap();
    }
}
