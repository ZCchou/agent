//! Finite Windows commands within capability-free, per-invocation AppContainers.
use crate::file_access::{AuthorizedRoot, FileAccessPolicy, VerifiedReadHandle};
use crate::file_search::Cancellation;
#[cfg(windows)]
use crate::offline_terminal::Process as Child;
use base64::Engine;
use serde::{Deserialize, Serialize};
#[cfg(not(windows))]
use std::process::Child;
use std::{
    io::{Read, Write},
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
    Ok(system_directory()?.join("WindowsPowerShell/v1.0/powershell.exe"))
}

#[cfg(windows)]
pub(crate) fn system_directory() -> Result<std::path::PathBuf, String> {
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
    Ok(std::path::PathBuf::from(std::ffi::OsString::from_wide(
        &directory[..size],
    )))
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
        pub fn attach(&self, child: &crate::offline_terminal::Process) -> Result<(), String> {
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
        let cwd = Self::pin_cwd(root, request)?;
        let project =
            FileAccessPolicy.open_read(root, &FileAccessPolicy.validate_relative(".")?)?;
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
try { Set-Location -LiteralPath $env:AGENT_TERMINAL_CWD -ErrorAction Stop } catch { [Console]::Error.WriteLine(('project location '+$env:AGENT_TERMINAL_CWD+': '+$_.ToString())); exit 125 };
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
        let child = Child::suspended(
            &shell,
            bootstrap,
            project.path(),
            &root.protected_root,
            cwd.path(),
        )?;
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
        process.child.resume()?;
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
    fn offline_terminal_is_appcontainer_and_denies_network_and_ungranted_files() {
        let (root, scope) = scope();
        let outside =
            std::env::temp_dir().join(format!("agent-secret-{}.txt", uuid::Uuid::new_v4()));
        std::fs::write(&outside, "outside-secret").unwrap();
        let command = format!(
            r#"
try {{ [IO.File]::ReadAllText('{}'); Write-Output 'FILE_ESCAPED' }} catch {{ Write-Output 'FILE_DENIED' }};
try {{ $client=New-Object Net.Sockets.TcpClient; $task=$client.ConnectAsync('1.1.1.1',80); if ($task.Wait(2000) -and $client.Connected) {{ Write-Output 'NETWORK_ESCAPED' }} else {{ Write-Output 'NETWORK_DENIED' }} }} catch {{ Write-Output 'NETWORK_DENIED' }};
Set-Content local.txt 'allowed'; Get-Content local.txt
"#,
            outside.display()
        );
        let result = TerminalProcess::start(&scope, &request(&command))
            .unwrap()
            .wait(Cancellation::default())
            .unwrap();
        assert!(!result.stdout.contains("FILE_ESCAPED"), "{result:?}");
        assert!(!result.stdout.contains("NETWORK_ESCAPED"), "{result:?}");
        assert!(result.stdout.contains("allowed"), "{result:?}");
        std::fs::remove_file(outside).unwrap();
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn offline_terminal_denies_http_dns_socket_and_child_network() {
        let (root, scope) = scope();
        let dns_receiver = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
        dns_receiver
            .set_read_timeout(Some(Duration::from_millis(250)))
            .unwrap();
        let dns_port = dns_receiver.local_addr().unwrap().port();
        let child = "try { $c=New-Object Net.Sockets.TcpClient; $c.Connect('1.1.1.1',80); Write-Output 'CHILD_ESCAPED' } catch { Write-Output 'CHILD_DENIED' }";
        let encoded = base64::engine::general_purpose::STANDARD.encode(
            child
                .encode_utf16()
                .flat_map(u16::to_le_bytes)
                .collect::<Vec<_>>(),
        );
        let command = format!(
            r#"
try {{ Invoke-WebRequest 'http://1.1.1.1' -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop; Write-Output 'HTTP_ESCAPED' }} catch {{ Write-Output 'HTTP_DENIED' }};
try {{ $udp=New-Object Net.Sockets.UdpClient; $udp.Connect('127.0.0.1',{dns_port}); [void]$udp.Send([byte[]](1),1); Write-Output 'DNS_SOCKET_BUFFERED' }} catch {{ Write-Output 'DNS_SOCKET_DENIED' }};
& powershell.exe -NoProfile -NonInteractive -EncodedCommand '{encoded}';
"#
        );
        let result = TerminalProcess::start(&scope, &request(&command))
            .unwrap()
            .wait(Cancellation::default())
            .unwrap();
        for marker in ["HTTP_DENIED", "CHILD_DENIED"] {
            assert!(result.stdout.contains(marker), "{result:?}");
        }
        assert!(!result.stdout.contains("ESCAPED"), "{result:?}");
        let mut datagram = [0u8; 8];
        assert!(
            dns_receiver.recv_from(&mut datagram).is_err(),
            "Isolated UDP reached the receiver"
        );
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn dns_resolver_api_cannot_reach_a_controlled_dns_server() {
        use std::process::{Command, Stdio};
        use std::sync::atomic::AtomicBool;
        let server = std::net::UdpSocket::bind("127.0.0.1:53")
            .expect("DNS isolation test requires unused loopback UDP53");
        server
            .set_read_timeout(Some(Duration::from_millis(100)))
            .unwrap();
        let control_name = format!("control-{}", uuid::Uuid::new_v4().simple());
        let isolated_name = format!("isolated-{}", uuid::Uuid::new_v4().simple());
        let observed_control = Arc::new(AtomicBool::new(false));
        let observed_isolated = Arc::new(AtomicBool::new(false));
        let stopped = Arc::new(AtomicBool::new(false));
        let flags = (
            observed_control.clone(),
            observed_isolated.clone(),
            stopped.clone(),
        );
        let names = (control_name.clone(), isolated_name.clone());
        let receiver = std::thread::spawn(move || {
            let mut packet = [0u8; 4096];
            while !flags.2.load(Ordering::SeqCst) {
                if let Ok((size, sender)) = server.recv_from(&mut packet) {
                    if size < 12 {
                        continue;
                    }
                    let control = packet[..size]
                        .windows(names.0.len())
                        .any(|value| value == names.0.as_bytes());
                    let isolated = packet[..size]
                        .windows(names.1.len())
                        .any(|value| value == names.1.as_bytes());
                    if control {
                        flags.0.store(true, Ordering::SeqCst);
                    }
                    if isolated {
                        flags.1.store(true, Ordering::SeqCst);
                    }
                    if control || isolated {
                        packet[2] = 0x81;
                        packet[3] = 0x83;
                        packet[6..12].fill(0);
                        let _ = server.send_to(&packet[..size], sender);
                    }
                }
            }
        });
        let query = |name: &str| {
            format!("try {{ Resolve-DnsName '{name}.example.com.' -Server 127.0.0.1 -DnsOnly -NoHostsFile -QuickTimeout -ErrorAction Stop | Out-Null }} catch {{ Write-Output 'DNS query finished' }}")
        };
        let control = Command::new(powershell_path().unwrap())
            .args([
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                &query(&control_name),
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        let (root, scope) = scope();
        let mut req = request(&query(&isolated_name));
        req.timeout_ms = 10000;
        let isolated = TerminalProcess::start(&scope, &req)
            .and_then(|process| process.wait(Cancellation::default()));
        stopped.store(true, Ordering::SeqCst);
        receiver.join().unwrap();
        assert!(
            control.is_ok() && observed_control.load(Ordering::SeqCst),
            "Unisolated control must reach the DNS receiver"
        );
        assert!(isolated.is_ok(), "{isolated:?}");
        assert!(
            !observed_isolated.load(Ordering::SeqCst),
            "AppContainer DNS API reached the receiving service"
        );
        std::fs::remove_dir_all(root).unwrap();
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
    use std::process::{Command, Stdio};
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
