//! Windows Core transport. The spawned child's exact handle is retained for shutdown.
//! Publication linearizes at successful bounded enqueue, never at a blocking pipe write.
use std::{
    io::Read,
    os::windows::io::{AsHandle, AsRawHandle, OwnedHandle},
    process::Command,
    sync::Arc,
};
use tauri::async_runtime::{channel, Receiver, Sender};
use tauri_plugin_shell::process::{CommandEvent, TerminatedPayload};
use windows_sys::Win32::{
    Foundation::WAIT_OBJECT_0,
    System::Threading::{TerminateProcess, WaitForSingleObject},
};

#[path = "core_transport_queue.rs"]
mod queue;
use queue::WriteQueue;

pub struct CoreTransport {
    queue: Arc<WriteQueue>,
    process: Arc<OwnedHandle>,
    pid: u32,
}

fn terminate(process: &OwnedHandle) -> Result<(), String> {
    // This is a duplicate of Child's actual process handle, never a PID lookup.
    let handle = process.as_raw_handle();
    if unsafe { WaitForSingleObject(handle, 0) } == WAIT_OBJECT_0 {
        return Ok(());
    }
    if unsafe { TerminateProcess(handle, 1) } == 0 {
        return Err("core_transport_shutdown_failed: Core could not be stopped".into());
    }
    Ok(())
}

impl CoreTransport {
    pub fn spawn(mut command: Command) -> Result<(Receiver<CommandEvent>, Self), String> {
        // The shell plugin's Into<Command> preserves the resolved sidecar executable,
        // environment, piped stdio and CREATE_NO_WINDOW flags.
        let mut child = command
            .spawn()
            .map_err(|_| "core_transport_spawn_failed: Core could not start".to_string())?;
        let process = match child.as_handle().try_clone_to_owned() {
            Ok(handle) => Arc::new(handle),
            Err(_) => {
                let _ = child.kill();
                return Err(
                    "core_transport_identity_failed: Core ownership could not be retained".into(),
                );
            }
        };
        let (Some(stdin), Some(stdout), Some(stderr)) =
            (child.stdin.take(), child.stdout.take(), child.stderr.take())
        else {
            let _ = child.kill();
            return Err("core_transport_spawn_failed: Core pipes are unavailable".into());
        };
        let pid = child.id();
        // Includes the message currently being written. Large attachment requests
        // cannot turn a stalled Core into an unbounded Host allocation.
        let queue = Arc::new(WriteQueue::new(32, 64 * 1024 * 1024));
        let transport = Self {
            queue: queue.clone(),
            process: process.clone(),
            pid,
        };
        let (sender, receiver) = channel(32);
        let stdout_reader = reader(stdout, sender.clone(), CommandEvent::Stdout);
        let stderr_reader = reader(stderr, sender.clone(), CommandEvent::Stderr);
        let writer_queue = queue.clone();
        std::thread::spawn(move || {
            writer_queue.run(stdin, || {
                let _ = terminate(&process);
            })
        });
        std::thread::spawn(move || {
            let status = child.wait();
            queue.close();
            // Drain final process output before emitting Terminated, as the previous
            // shell adapter did. UI shutdown never joins these reader/writer threads.
            let _ = stdout_reader.join();
            let _ = stderr_reader.join();
            let event = match status {
                Ok(status) => CommandEvent::Terminated(TerminatedPayload {
                    code: status.code(),
                    signal: None,
                }),
                Err(_) => CommandEvent::Error("core_transport_wait_failed".into()),
            };
            let _ = sender.blocking_send(event);
        });
        Ok((receiver, transport))
    }
    pub fn pid(&self) -> u32 {
        self.pid
    }
    pub fn write(&mut self, id: &str, bytes: &[u8]) -> Result<(), String> {
        self.queue.push(id, bytes)
    }
    pub fn cancel_request(&self, id: &str) {
        self.queue.cancel(id);
    }
    pub fn kill(self) -> Result<(), String> {
        self.queue.close();
        terminate(&self.process)
    }
    pub fn close_stdin_and_wait(&self, milliseconds: u32) -> Result<(), String> {
        self.queue.close();
        if unsafe { WaitForSingleObject(self.process.as_raw_handle(), milliseconds) }
            == WAIT_OBJECT_0
        {
            Ok(())
        } else {
            Err("app_update_core_unavailable: Core has not finished shutting down.".into())
        }
    }
}
impl Drop for CoreTransport {
    fn drop(&mut self) {
        self.queue.close();
        let _ = terminate(&self.process);
    }
}

fn reader(
    mut pipe: impl Read + Send + 'static,
    sender: Sender<CommandEvent>,
    event: fn(Vec<u8>) -> CommandEvent,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        let mut buffer = [0u8; 16384];
        loop {
            match pipe.read(&mut buffer) {
                Ok(0) => return,
                Ok(count) => {
                    if sender
                        .blocking_send(event(buffer[..count].to_vec()))
                        .is_err()
                    {
                        return;
                    }
                }
                Err(_) => {
                    let _ = sender
                        .blocking_send(CommandEvent::Error("core_transport_read_failed".into()));
                    return;
                }
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{io::Write, os::windows::process::CommandExt, process::Stdio, time::Duration};

    #[test]
    #[ignore = "finite native subprocess fixture; invoked by graceful EOF test"]
    fn graceful_stdin_fixture_child() {
        let Some(marker) = std::env::var_os("AGENT_TEST_EOF_READY") else {
            return;
        };
        // libtest's --nocapture preamble may be on the same line.
        println!("\n{}", marker.to_string_lossy());
        std::io::stdout().flush().unwrap();
        let mut data = Vec::new();
        std::io::stdin().read_to_end(&mut data).unwrap();
    }

    #[tokio::test]
    async fn update_shutdown_closes_stdin_and_waits_for_owned_child_exit() {
        let marker = format!("eof-ready-{}", uuid::Uuid::new_v4());
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "core_transport::tests::graceful_stdin_fixture_child",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .env("AGENT_TEST_EOF_READY", &marker)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .creation_flags(0x0800_0000);
        let (mut events, transport) = CoreTransport::spawn(command).unwrap();
        tokio::time::timeout(
            Duration::from_secs(10),
            wait_for_ready(&mut events, &marker),
        )
        .await
        .unwrap();
        transport.close_stdin_and_wait(5000).unwrap();
        assert_eq!(
            unsafe { WaitForSingleObject(transport.process.as_raw_handle(), 0) },
            WAIT_OBJECT_0
        );
    }

    async fn wait_for_ready(events: &mut Receiver<CommandEvent>, marker: &str) {
        let mut pending = Vec::new();
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Stdout(bytes) => {
                    pending.extend_from_slice(&bytes);
                    assert!(
                        pending.len() <= 65536,
                        "native fixture output exceeded its bound"
                    );
                    while let Some(end) = pending.iter().position(|byte| *byte == b'\n') {
                        let line: Vec<_> = pending.drain(..=end).collect();
                        let line = &line[..line.len() - 1];
                        if line.strip_suffix(b"\r").unwrap_or(line) == marker.as_bytes() {
                            return;
                        }
                    }
                }
                CommandEvent::Terminated(status) => {
                    panic!("native fixture exited before ready: {:?}", status.code);
                }
                CommandEvent::Error(error) => panic!("native fixture failed before ready: {error}"),
                _ => {}
            }
        }
        panic!("synthetic child exited before ready");
    }

    #[test]
    #[ignore = "finite native subprocess fixture; started explicitly by the owned-handle test"]
    fn blocked_stdin_fixture_child() {
        let marker = std::env::var("AGENT_TEST_TRANSPORT_READY")
            .expect("fixture requires its owning test's readiness marker");
        // A leading newline separates our marker from libtest's --nocapture preamble.
        let mut output = std::io::stdout().lock();
        writeln!(output, "\n{marker}").unwrap();
        output.flush().unwrap();
        drop(output);
        // Never read stdin. The fixture is finite even if its owning test crashes.
        std::thread::sleep(Duration::from_secs(30));
    }

    #[tokio::test]
    async fn core_transport_fixture_waits_for_exact_ready_line_across_chunks() {
        let (sender, mut events) = channel(8);
        let marker = "agent-transport-ready-precise";
        sender
            .send(CommandEvent::Stdout(
                b"libtest preamble\nagent-transport-ready-precise-suffix\n".to_vec(),
            ))
            .await
            .unwrap();
        sender
            .send(CommandEvent::Stderr(format!("{marker}\n").into_bytes()))
            .await
            .unwrap();
        sender
            .send(CommandEvent::Stdout(b"agent-transport-ready-".to_vec()))
            .await
            .unwrap();
        let mut ready = Box::pin(wait_for_ready(&mut events, marker));
        assert!(
            tokio::time::timeout(Duration::from_millis(20), &mut ready)
                .await
                .is_err(),
            "preamble, stderr and an incomplete marker must not release the fixture"
        );
        sender
            .send(CommandEvent::Stdout(b"precise\r\n".to_vec()))
            .await
            .unwrap();
        tokio::time::timeout(Duration::from_secs(2), ready)
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn core_transport_shutdown_interrupts_blocked_writer_using_owned_handle() {
        // Reuse the already-built native test executable; no PATH interpreter/.NET startup.
        let marker = format!("agent-transport-ready-{}", uuid::Uuid::new_v4());
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "core_transport::tests::blocked_stdin_fixture_child",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .env("AGENT_TEST_TRANSPORT_READY", &marker)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .creation_flags(0x0800_0000);
        let (mut events, mut transport) = CoreTransport::spawn(command).unwrap();
        tokio::time::timeout(
            Duration::from_secs(10),
            wait_for_ready(&mut events, &marker),
        )
        .await
        .expect("native child did not emit its exact readiness marker");
        let process = transport.process.clone();
        let queue = transport.queue.clone();
        transport
            .write("blocked", &vec![b'x'; 1024 * 1024])
            .unwrap();
        tokio::time::timeout(Duration::from_secs(5), async {
            while queue::in_flight_count(&queue) == 0 {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .expect("writer never claimed the payload for the child's non-reading stdin");
        assert_eq!(queue::in_flight_count(&queue), 1);
        assert_eq!(
            unsafe { WaitForSingleObject(process.as_raw_handle(), 0) },
            windows_sys::Win32::Foundation::WAIT_TIMEOUT
        );
        transport.kill().unwrap();
        assert!(queue.push("late", b"late").is_err());
        tokio::time::timeout(Duration::from_secs(5), async {
            while let Some(event) = events.recv().await {
                if matches!(event, CommandEvent::Terminated(_)) {
                    return;
                }
            }
            panic!("synthetic child did not emit termination");
        })
        .await
        .unwrap();
        assert_eq!(
            unsafe { WaitForSingleObject(process.as_raw_handle(), 0) },
            WAIT_OBJECT_0
        );
        tokio::time::timeout(Duration::from_secs(5), async {
            while queue::in_flight_count(&queue) != 0 {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .expect("owned-handle shutdown did not release the blocked stdin writer");
    }
}
