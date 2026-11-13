//! Postflight is created suspended, journaled, and confined to a kill-on-close job.
//! Recovery waits for the recorded child and complete job before touching files.
#![cfg(windows)]
use serde::{Deserialize, Serialize};
use std::{
    ffi::OsStr,
    fs::File,
    os::windows::{
        ffi::OsStrExt,
        io::{AsRawHandle, FromRawHandle, OwnedHandle},
    },
    path::Path,
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::*,
    System::{JobObjects::*, Threading::*},
};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
#[derive(Serialize, Deserialize)]
pub struct Identity {
    pub pid: u32,
    pub started: u64,
    pub job: String,
}
pub struct Child {
    process: OwnedHandle,
    thread: OwnedHandle,
    job: OwnedHandle,
    identity: Identity,
}
fn wide(value: &OsStr) -> Vec<u16> {
    value.encode_wide().chain(Some(0)).collect()
}
fn job_name(id: &str) -> String {
    format!("Local\\agent-offline-postflight-{id}")
}
fn quote(value: &str) -> String {
    let mut result = String::from("\"");
    let mut slashes = 0;
    for c in value.chars() {
        if c == '\\' {
            slashes += 1;
            continue;
        }
        result.extend(std::iter::repeat_n(
            '\\',
            if c == '"' { slashes * 2 + 1 } else { slashes },
        ));
        result.push(c);
        slashes = 0;
    }
    result.extend(std::iter::repeat_n('\\', slashes * 2));
    result.push('"');
    result
}
fn started(process: HANDLE) -> Result<u64> {
    let mut a: FILETIME = unsafe { std::mem::zeroed() };
    let mut b = a;
    let mut c = a;
    let mut d = a;
    if unsafe { GetProcessTimes(process, &mut a, &mut b, &mut c, &mut d) } == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(((a.dwHighDateTime as u64) << 32) | a.dwLowDateTime as u64)
}
fn duplicate(handle: HANDLE) -> Result<OwnedHandle> {
    let mut result = std::ptr::null_mut();
    if unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            handle,
            GetCurrentProcess(),
            &mut result,
            0,
            1,
            DUPLICATE_SAME_ACCESS,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(unsafe { OwnedHandle::from_raw_handle(result) })
}
fn active(job: HANDLE) -> Result<u32> {
    let mut accounting: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe {
        QueryInformationJobObject(
            job,
            JobObjectBasicAccountingInformation,
            &mut accounting as *mut _ as *mut _,
            std::mem::size_of_val(&accounting) as u32,
            std::ptr::null_mut(),
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(accounting.ActiveProcesses)
}
fn stop_and_wait(job: HANDLE) -> Result<()> {
    if unsafe { TerminateJobObject(job, 1) } == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let deadline = Instant::now() + Duration::from_secs(30);
    while active(job)? != 0 {
        if Instant::now() >= deadline {
            return Err("postflight process tree is still stopping; recovery blocked".into());
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    Ok(())
}
impl Child {
    pub fn spawn(executable: &Path, arguments: &[String], transaction_id: &str) -> Result<Self> {
        let name = job_name(transaction_id);
        let name_wide = wide(OsStr::new(&name));
        let job = unsafe { CreateJobObjectW(std::ptr::null(), name_wide.as_ptr()) };
        if job.is_null() {
            return Err(std::io::Error::last_os_error().into());
        }
        let job = unsafe { OwnedHandle::from_raw_handle(job) };
        if unsafe { GetLastError() } == ERROR_ALREADY_EXISTS {
            return Err("postflight job identity already exists".into());
        }
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
            return Err(std::io::Error::last_os_error().into());
        }
        let input = File::open("NUL")?;
        let input = duplicate(input.as_raw_handle())?;
        let output = duplicate(std::io::stdout().as_raw_handle())?;
        let error = duplicate(std::io::stderr().as_raw_handle())?;
        let mut startup: STARTUPINFOW = unsafe { std::mem::zeroed() };
        startup.cb = std::mem::size_of_val(&startup) as u32;
        startup.dwFlags = STARTF_USESTDHANDLES;
        startup.hStdInput = input.as_raw_handle();
        startup.hStdOutput = output.as_raw_handle();
        startup.hStdError = error.as_raw_handle();
        let executable_wide = wide(executable.as_os_str());
        let command = std::iter::once(executable.to_string_lossy().into_owned())
            .chain(arguments.iter().cloned())
            .map(|v| quote(&v))
            .collect::<Vec<_>>()
            .join(" ");
        let mut command = wide(OsStr::new(&command));
        let mut info: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
        if unsafe {
            CreateProcessW(
                executable_wide.as_ptr(),
                command.as_mut_ptr(),
                std::ptr::null(),
                std::ptr::null(),
                1,
                CREATE_SUSPENDED | CREATE_NO_WINDOW,
                std::ptr::null(),
                std::ptr::null(),
                &startup,
                &mut info,
            )
        } == 0
        {
            return Err(std::io::Error::last_os_error().into());
        }
        let process = unsafe { OwnedHandle::from_raw_handle(info.hProcess) };
        let thread = unsafe { OwnedHandle::from_raw_handle(info.hThread) };
        if unsafe { AssignProcessToJobObject(job.as_raw_handle(), process.as_raw_handle()) } == 0 {
            unsafe {
                TerminateProcess(process.as_raw_handle(), 1);
                WaitForSingleObject(process.as_raw_handle(), 30_000);
            }
            return Err("could not bind postflight to installer lifetime".into());
        }
        let identity = Identity {
            pid: info.dwProcessId,
            started: started(process.as_raw_handle())?,
            job: name,
        };
        Ok(Self {
            process,
            thread,
            job,
            identity,
        })
    }
    pub fn identity(&self) -> &Identity {
        &self.identity
    }
    pub fn resume_and_wait(&self) -> Result<u32> {
        if unsafe { ResumeThread(self.thread.as_raw_handle()) } == u32::MAX {
            return Err(std::io::Error::last_os_error().into());
        }
        // Keep the named job and process handles until every descendant has stopped.
        while active(self.job.as_raw_handle())? != 0 {
            std::thread::sleep(Duration::from_millis(50));
        }
        let mut code = 0;
        if unsafe { GetExitCodeProcess(self.process.as_raw_handle(), &mut code) } == 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        Ok(code)
    }
}
impl Drop for Child {
    fn drop(&mut self) {
        let _ = stop_and_wait(self.job.as_raw_handle());
    }
}

pub fn recover(identity: &Identity, transaction_id: &str) -> Result<()> {
    if identity.job != job_name(transaction_id) {
        return Err("postflight ownership differs from transaction".into());
    }
    let name = wide(OsStr::new(&identity.job));
    let job = unsafe { OpenJobObjectW(0x0004 | 0x0008, 0, name.as_ptr()) }; // QUERY | TERMINATE
    if !job.is_null() {
        let job = unsafe { OwnedHandle::from_raw_handle(job) };
        stop_and_wait(job.as_raw_handle())?;
    } else if unsafe { GetLastError() } != ERROR_FILE_NOT_FOUND {
        return Err(std::io::Error::last_os_error().into());
    }
    let process = unsafe {
        OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | 0x00100000,
            0,
            identity.pid,
        )
    };
    if process.is_null() {
        if unsafe { GetLastError() } == ERROR_INVALID_PARAMETER {
            return Ok(());
        }
        return Err(std::io::Error::last_os_error().into());
    }
    let process = unsafe { OwnedHandle::from_raw_handle(process) };
    if started(process.as_raw_handle())? == identity.started
        && unsafe { WaitForSingleObject(process.as_raw_handle(), 30_000) } != WAIT_OBJECT_0
    {
        return Err("postflight child is still alive; recovery blocked".into());
    }
    Ok(())
}
