//! Own the upgraded Host and its entire process tree during the health handshake.
#[cfg(windows)]
mod windows {
    use std::{
        collections::BTreeMap,
        ffi::OsString,
        os::windows::{
            ffi::OsStrExt,
            io::{AsRawHandle, FromRawHandle, OwnedHandle},
        },
        path::Path,
        time::{Duration, Instant},
    };
    use windows_sys::Win32::{
        Foundation::{WAIT_OBJECT_0, WAIT_TIMEOUT},
        System::{JobObjects::*, Threading::*},
    };

    fn wide(value: &std::ffi::OsStr) -> Vec<u16> {
        value.encode_wide().chain(Some(0)).collect()
    }

    pub struct ManagedHost {
        process: OwnedHandle,
        job: OwnedHandle,
    }

    impl ManagedHost {
        pub fn spawn(
            executable: &Path,
            arguments: &str,
            overrides: &[(&str, &str)],
        ) -> Result<Self, String> {
            let job = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
            if job.is_null() {
                return Err("app_update_process_failed".into());
            }
            let job = unsafe { OwnedHandle::from_raw_handle(job) };
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if unsafe {
                SetInformationJobObject(
                    job.as_raw_handle(),
                    JobObjectExtendedLimitInformation,
                    &limits as *const _ as *const _,
                    std::mem::size_of_val(&limits) as u32,
                )
            } == 0
            {
                return Err("app_update_process_failed".into());
            }
            let mut environment: BTreeMap<OsString, OsString> = std::env::vars_os().collect();
            environment.retain(|key, _| {
                !overrides
                    .iter()
                    .any(|(name, _)| key.to_string_lossy().eq_ignore_ascii_case(name))
            });
            for (key, value) in overrides {
                environment.insert((*key).into(), (*value).into());
            }
            let mut block = Vec::new();
            for (key, value) in environment {
                block.extend(key.encode_wide());
                block.push('=' as u16);
                block.extend(value.encode_wide());
                block.push(0);
            }
            block.push(0);
            let application = wide(executable.as_os_str());
            let mut command = wide(std::ffi::OsStr::new(&format!(
                "\"{}\" {}",
                executable.display(),
                arguments
            )));
            let directory = wide(
                executable
                    .parent()
                    .ok_or("app_update_process_failed")?
                    .as_os_str(),
            );
            let mut startup: STARTUPINFOW = unsafe { std::mem::zeroed() };
            startup.cb = std::mem::size_of_val(&startup) as u32;
            let mut information: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
            if unsafe {
                CreateProcessW(
                    application.as_ptr(),
                    command.as_mut_ptr(),
                    std::ptr::null(),
                    std::ptr::null(),
                    0,
                    CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
                    block.as_ptr() as *const _,
                    directory.as_ptr(),
                    &startup,
                    &mut information,
                )
            } == 0
            {
                return Err("app_update_process_failed".into());
            }
            let process = unsafe { OwnedHandle::from_raw_handle(information.hProcess) };
            let thread = unsafe { OwnedHandle::from_raw_handle(information.hThread) };
            if unsafe { AssignProcessToJobObject(job.as_raw_handle(), process.as_raw_handle()) }
                == 0
            {
                unsafe {
                    TerminateProcess(process.as_raw_handle(), 1);
                    WaitForSingleObject(process.as_raw_handle(), 5000);
                }
                return Err("app_update_process_failed".into());
            }
            let managed = Self { process, job };
            if unsafe { ResumeThread(thread.as_raw_handle()) } == u32::MAX {
                return Err("app_update_process_failed".into());
            }
            Ok(managed)
        }

        pub fn has_exited(&self) -> Result<bool, String> {
            match unsafe { WaitForSingleObject(self.process.as_raw_handle(), 0) } {
                WAIT_OBJECT_0 => Ok(true),
                WAIT_TIMEOUT => Ok(false),
                _ => Err("app_update_process_failed".into()),
            }
        }

        pub fn terminate_tree(&mut self) -> Result<(), String> {
            if unsafe { TerminateJobObject(self.job.as_raw_handle(), 1) } == 0 {
                return Err("app_update_process_failed".into());
            }
            let deadline = Instant::now() + Duration::from_secs(10);
            loop {
                let mut accounting: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION =
                    unsafe { std::mem::zeroed() };
                if unsafe {
                    QueryInformationJobObject(
                        self.job.as_raw_handle(),
                        JobObjectBasicAccountingInformation,
                        &mut accounting as *mut _ as *mut _,
                        std::mem::size_of_val(&accounting) as u32,
                        std::ptr::null_mut(),
                    )
                } == 0
                {
                    return Err("app_update_process_failed".into());
                }
                if accounting.ActiveProcesses == 0 && self.has_exited()? {
                    return Ok(());
                }
                if Instant::now() >= deadline {
                    return Err("app_update_process_failed".into());
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        }
    }

    impl Drop for ManagedHost {
        fn drop(&mut self) {
            let _ = self.terminate_tree();
        }
    }

    pub struct Parent(OwnedHandle);
    impl Parent {
        pub fn open(pid: u32, executable: &Path) -> Result<Self, String> {
            let process = unsafe {
                OpenProcess(
                    PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                    0,
                    pid,
                )
            };
            if process.is_null() {
                return Err("app_update_parent_unavailable".into());
            }
            let process = unsafe { OwnedHandle::from_raw_handle(process) };
            let mut buffer = vec![0_u16; 32768];
            let mut size = buffer.len() as u32;
            if unsafe {
                QueryFullProcessImageNameW(
                    process.as_raw_handle(),
                    0,
                    buffer.as_mut_ptr(),
                    &mut size,
                )
            } == 0
            {
                return Err("app_update_parent_unavailable".into());
            }
            let actual =
                std::path::PathBuf::from(String::from_utf16_lossy(&buffer[..size as usize]));
            if actual.canonicalize().ok() != executable.canonicalize().ok() {
                return Err("app_update_parent_unavailable".into());
            }
            Ok(Self(process))
        }
        pub fn wait(&self) -> Result<(), String> {
            if unsafe { WaitForSingleObject(self.0.as_raw_handle(), 120_000) } == WAIT_OBJECT_0 {
                Ok(())
            } else {
                Err("app_update_parent_busy".into())
            }
        }
    }
}

#[cfg(windows)]
pub use windows::{ManagedHost, Parent};

#[cfg(all(test, windows))]
mod tests {
    use super::*;
    #[test]
    fn managed_host_starts_in_job_and_termination_waits_for_the_tree() {
        let powershell = std::path::PathBuf::from(std::env::var_os("SystemRoot").unwrap())
            .join("System32/WindowsPowerShell/v1.0/powershell.exe");
        let mut child = ManagedHost::spawn(
            &powershell,
            "-NoLogo -NoProfile -NonInteractive -Command \"Start-Sleep -Seconds 30\"",
            &[],
        )
        .unwrap();
        assert!(!child.has_exited().unwrap());
        child.terminate_tree().unwrap();
        assert!(child.has_exited().unwrap());
    }
}
