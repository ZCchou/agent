//! An ephemeral, capability-free AppContainer for each terminal invocation.
use std::{
    ffi::OsStr,
    fs::File,
    os::windows::{
        ffi::OsStrExt,
        io::{AsRawHandle, FromRawHandle, OwnedHandle},
    },
    path::{Path, PathBuf},
    ptr::{null, null_mut},
};
use windows_sys::Win32::{
    Foundation::*,
    Security::{Authorization::*, Isolation::*, *},
    Storage::FileSystem::*,
    System::{Pipes::CreatePipe, Threading::*},
};

fn wide(value: impl AsRef<OsStr>) -> Vec<u16> {
    value.as_ref().encode_wide().chain(Some(0)).collect()
}
fn failure(operation: &str) -> String {
    format!(
        "terminal_isolation_unavailable: {operation}: {}",
        std::io::Error::last_os_error()
    )
}

#[link(name = "ntdll")]
extern "system" {
    fn NtCreateSymbolicLinkObject(
        handle: *mut HANDLE,
        access: u32,
        attributes: *const windows_sys::Wdk::Foundation::OBJECT_ATTRIBUTES,
        target: *const UNICODE_STRING,
    ) -> NTSTATUS;
}

struct ProjectDrive {
    _handle: OwnedHandle,
    path: PathBuf,
}
impl ProjectDrive {
    fn new(root: &Path) -> Result<Self, String> {
        static DRIVE_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
        let _lock = DRIVE_LOCK
            .lock()
            .map_err(|_| failure("Reserve project drive"))?;
        let drives = unsafe { GetLogicalDrives() };
        let source = root.to_string_lossy();
        let source = source.strip_prefix(r"\\?\").unwrap_or(&source);
        if source.starts_with(r"\\") {
            return Err(
                "terminal_isolation_unavailable: A local project directory is required".into(),
            );
        }
        let mut target = wide(format!(r"\??\{source}"));
        let target = UNICODE_STRING {
            Length: ((target.len() - 1) * 2)
                .try_into()
                .map_err(|_| "terminal_isolation_unavailable: Project path too long")?,
            MaximumLength: (target.len() * 2)
                .try_into()
                .map_err(|_| "terminal_isolation_unavailable: Project path too long")?,
            Buffer: target.as_mut_ptr(),
        };
        for letter in (b'D'..=b'Z')
            .rev()
            .filter(|letter| drives & (1 << (letter - b'A')) == 0)
        {
            let mut name = wide(format!(r"\??\{}:", letter as char));
            let name = UNICODE_STRING {
                Length: ((name.len() - 1) * 2) as u16,
                MaximumLength: (name.len() * 2) as u16,
                Buffer: name.as_mut_ptr(),
            };
            let attributes = windows_sys::Wdk::Foundation::OBJECT_ATTRIBUTES {
                Length: std::mem::size_of::<windows_sys::Wdk::Foundation::OBJECT_ATTRIBUTES>()
                    as u32,
                RootDirectory: null_mut(),
                ObjectName: &name,
                Attributes: 0x40,
                SecurityDescriptor: null(),
                SecurityQualityOfService: null(),
            };
            let mut handle = null_mut();
            // No OBJ_PERMANENT/OPENIF. Existing mappings are never replaced, and
            // the OS removes this private mapping when its final handle closes.
            let status = unsafe {
                NtCreateSymbolicLinkObject(&mut handle, 0x000F0001, &attributes, &target)
            };
            if status >= 0 {
                return Ok(Self {
                    _handle: unsafe { OwnedHandle::from_raw_handle(handle) },
                    path: PathBuf::from(format!("{}:\\", letter as char)),
                });
            }
            if status as u32 != 0xC0000035 {
                return Err(format!(
                    "terminal_isolation_unavailable: Map project root NTSTATUS {status:#x}"
                ));
            }
        }
        Err("terminal_isolation_unavailable: No temporary project drive is available".into())
    }
}

#[derive(serde::Serialize, serde::Deserialize)]
struct ProfileRecord {
    name: String,
    root: PathBuf,
    temp: PathBuf,
    owner_pid: u32,
    owner_birth: u64,
    #[serde(default)]
    granted_paths: Vec<PathBuf>,
}

fn project_entries(
    root: &Path,
    protected: &Path,
) -> Result<Vec<crate::file_access::VerifiedReadHandle>, String> {
    use crate::file_access::{AuthorizedRoot, FileAccessPolicy};
    let canonical = root
        .canonicalize()
        .map_err(|_| failure("Resolve project tree"))?;
    let authority = AuthorizedRoot::for_host_path(&canonical, protected);
    let mut entries =
        vec![FileAccessPolicy.open_read(&authority, &FileAccessPolicy.validate_relative(".")?)?];
    let mut index = 0;
    while index < entries.len() {
        if entries[index].is_dir() {
            for name in entries[index].entries()? {
                let path = entries[index].path().join(name);
                let relative = path
                    .strip_prefix(entries[0].path())
                    .map_err(|_| failure("Resolve project entry"))?;
                let relative = match FileAccessPolicy.validate_relative(&relative.to_string_lossy())
                {
                    Ok(path) => path,
                    Err(error) if error.starts_with("forbidden_path:") => continue,
                    Err(error) => return Err(error),
                };
                match FileAccessPolicy.open_read(&authority, &relative) {
                    Ok(entry) => entries.push(entry),
                    // An exclusively owned file remains outside this terminal's
                    // grant set. No recursive grant can confer access to it.
                    Err(error) if error.starts_with("file_access_busy:") => continue,
                    Err(error) => return Err(error),
                }
                if entries.len() > 100_000 {
                    return Err(
                        "terminal_isolation_unavailable: Project contains more than 100000 entries"
                            .into(),
                    );
                }
            }
        }
        index += 1;
    }
    Ok(entries)
}

fn process_birth(handle: HANDLE) -> Option<u64> {
    let (mut created, mut exited, mut kernel, mut user) = (
        FILETIME::default(),
        FILETIME::default(),
        FILETIME::default(),
        FILETIME::default(),
    );
    if unsafe { GetProcessTimes(handle, &mut created, &mut exited, &mut kernel, &mut user) } == 0 {
        return None;
    }
    Some(((created.dwHighDateTime as u64) << 32) | created.dwLowDateTime as u64)
}

struct Profile {
    name: Vec<u16>,
    sid: PSID,
    grants: Vec<PathBuf>,
    temp: PathBuf,
    marker: Option<PathBuf>,
    record: ProfileRecord,
}
// The SID allocation is private and remains alive for the process lifetime.
unsafe impl Send for Profile {}
impl Profile {
    fn create(root: &Path, protected: &Path) -> Result<Self, String> {
        // Pin and validate every existing object before granting authority. ACLs
        // are applied without propagation, so a newly inserted outside alias
        // can never acquire the project SID during the grant pass.
        let entries = project_entries(root, protected)?;
        if !protected.as_os_str().is_empty() {
            let project = root
                .canonicalize()
                .map_err(|_| failure("Resolve project authority"))?;
            let protected = protected
                .canonicalize()
                .map_err(|_| failure("Resolve application-private storage"))?;
            let same = |left: &Path, right: &Path| {
                crate::file_access::windows_path_eq(
                    &left.to_string_lossy(),
                    &right.to_string_lossy(),
                )
            };
            if project.ancestors().any(|path| same(path, &protected))
                || protected.ancestors().any(|path| same(path, &project))
            {
                return Err(
                    "terminal_isolation_unavailable: Project overlaps application-private storage"
                        .into(),
                );
            }
        }
        let identity = uuid::Uuid::new_v4().simple().to_string();
        let record_name = format!("agent.airworthiness.offline.{identity}");
        let name = wide(&record_name);
        let state = if protected.as_os_str().is_empty() {
            None
        } else {
            Some(protected.join("terminal-isolation"))
        };
        if let Some(state) = &state {
            std::fs::create_dir_all(state)
                .map_err(|_| failure("Create isolation recovery directory"))?;
            Self::recover(state)?;
        }
        let mut sid = null_mut();
        let status = unsafe {
            CreateAppContainerProfile(
                name.as_ptr(),
                name.as_ptr(),
                name.as_ptr(),
                null(),
                0,
                &mut sid,
            )
        };
        if status < 0 {
            return Err(format!(
                "terminal_isolation_unavailable: CreateAppContainerProfile HRESULT {status:#x}"
            ));
        }
        let temp = std::env::temp_dir().join(format!("agent-offline-{identity}"));
        let record = ProfileRecord {
            name: record_name,
            root: root.to_path_buf(),
            temp: temp.clone(),
            owner_pid: std::process::id(),
            owner_birth: process_birth(unsafe { GetCurrentProcess() })
                .ok_or_else(|| failure("Read isolation owner identity"))?,
            granted_paths: entries
                .iter()
                .map(|entry| entry.path().to_path_buf())
                .chain(Some(temp.clone()))
                .collect(),
        };
        let mut profile = Self {
            name,
            sid,
            grants: vec![],
            temp,
            marker: state.map(|state| state.join(format!("{identity}.json"))),
            record,
        };
        profile.persist()?;
        std::fs::create_dir(&profile.temp)
            .map_err(|_| failure("Create private temporary directory"))?;
        for path in profile.record.granted_paths.clone() {
            profile.grants.push(path.clone());
            profile.acl(
                &path,
                GRANT_ACCESS,
                FILE_GENERIC_READ | FILE_GENERIC_WRITE | FILE_GENERIC_EXECUTE | DELETE,
                SUB_CONTAINERS_AND_OBJECTS_INHERIT,
            )?;
        }
        Ok(profile)
    }
    fn persist(&self) -> Result<(), String> {
        if let Some(marker) = &self.marker {
            let temporary = marker.with_extension("tmp");
            let payload = serde_json::to_vec(&self.record)
                .map_err(|_| failure("Encode isolation recovery"))?;
            if payload.len() > 16 * 1024 * 1024 {
                return Err(
                    "terminal_isolation_unavailable: Project recovery manifest exceeds 16 MiB"
                        .into(),
                );
            }
            std::fs::write(&temporary, payload).map_err(|_| failure("Write isolation recovery"))?;
            std::fs::rename(temporary, marker)
                .map_err(|_| failure("Publish isolation recovery"))?;
        }
        Ok(())
    }
    fn recover(directory: &Path) -> Result<(), String> {
        for entry in std::fs::read_dir(directory).map_err(|_| failure("Read isolation recovery"))? {
            let entry = entry.map_err(|_| failure("Read isolation recovery entry"))?;
            let marker = entry.path();
            if marker.extension().and_then(|extension| extension.to_str()) != Some("json")
                || entry
                    .metadata()
                    .map_or(true, |metadata| metadata.len() > 16 * 1024 * 1024)
            {
                continue;
            }
            let record: ProfileRecord = serde_json::from_slice(
                &std::fs::read(&marker).map_err(|_| failure("Read isolation record"))?,
            )
            .map_err(|_| "terminal_isolation_unavailable: Isolation recovery record is corrupt")?;
            let Some(identity) = record.name.strip_prefix("agent.airworthiness.offline.") else {
                continue;
            };
            if identity.len() != 32
                || !identity.bytes().all(|byte| byte.is_ascii_hexdigit())
                || record.temp.file_name().and_then(|name| name.to_str())
                    != Some(format!("agent-offline-{identity}").as_str())
            {
                continue;
            }
            let owner =
                unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, record.owner_pid) };
            if !owner.is_null() {
                let owner = unsafe { OwnedHandle::from_raw_handle(owner) };
                if process_birth(owner.as_raw_handle())
                    .is_none_or(|birth| birth == record.owner_birth)
                {
                    continue;
                }
            } else if unsafe { GetLastError() } == ERROR_ACCESS_DENIED {
                continue;
            }
            let name = wide(&record.name);
            let mut sid = null_mut();
            if unsafe { DeriveAppContainerSidFromAppContainerName(name.as_ptr(), &mut sid) } < 0 {
                continue;
            }
            let profile = Self {
                name,
                sid,
                grants: if record.granted_paths.is_empty() {
                    vec![record.root.clone(), record.temp.clone()]
                } else {
                    record.granted_paths.clone()
                },
                temp: record.temp.clone(),
                marker: Some(marker),
                record,
            };
            drop(profile);
        }
        Ok(())
    }
    fn acl(
        &self,
        path: &Path,
        mode: ACCESS_MODE,
        permissions: u32,
        inheritance: ACE_FLAGS,
    ) -> Result<(), String> {
        let path = wide(path);
        unsafe {
            let mut old = null_mut();
            let mut descriptor = null_mut();
            let status = GetNamedSecurityInfoW(
                path.as_ptr(),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION,
                null_mut(),
                null_mut(),
                &mut old,
                null_mut(),
                &mut descriptor,
            );
            if status != 0 {
                return Err(format!(
                    "terminal_isolation_unavailable: Read directory ACL {status}"
                ));
            }
            let entry = EXPLICIT_ACCESS_W {
                grfAccessPermissions: permissions,
                grfAccessMode: mode,
                grfInheritance: inheritance,
                Trustee: TRUSTEE_W {
                    TrusteeForm: TRUSTEE_IS_SID,
                    TrusteeType: TRUSTEE_IS_UNKNOWN,
                    ptstrName: self.sid.cast(),
                    ..Default::default()
                },
            };
            let mut updated = null_mut();
            let status = SetEntriesInAclW(1, &entry, old, &mut updated);
            let result = if status == 0 {
                // SetFileSecurity deliberately avoids propagation onto existing
                // descendants. Directory ACEs still apply to future new children.
                let mut absolute: SECURITY_DESCRIPTOR = std::mem::zeroed();
                if InitializeSecurityDescriptor(
                    (&mut absolute as *mut SECURITY_DESCRIPTOR).cast(),
                    1,
                ) == 0
                    || SetSecurityDescriptorDacl(
                        (&mut absolute as *mut SECURITY_DESCRIPTOR).cast(),
                        1,
                        updated,
                        0,
                    ) == 0
                    || SetFileSecurityW(
                        path.as_ptr(),
                        DACL_SECURITY_INFORMATION,
                        (&mut absolute as *mut SECURITY_DESCRIPTOR).cast(),
                    ) == 0
                {
                    GetLastError()
                } else {
                    0
                }
            } else {
                status
            };
            if !updated.is_null() {
                LocalFree(updated.cast());
            }
            LocalFree(descriptor.cast());
            if result != 0 {
                return Err(format!(
                    "terminal_isolation_unavailable: Update directory ACL {result}"
                ));
            }
        }
        Ok(())
    }
}
impl Drop for Profile {
    fn drop(&mut self) {
        let mut clean = true;
        // Include children created by the isolated command. Invalid aliases are
        // never followed during cleanup, and no grant is performed here.
        for root in [&self.record.root, &self.temp] {
            if let Ok(entries) = project_entries(root, Path::new("")) {
                for entry in entries {
                    if !self.grants.contains(&entry.path().to_path_buf()) {
                        self.grants.push(entry.path().to_path_buf());
                    }
                }
            }
        }
        for path in self.grants.iter().rev() {
            if path.exists() {
                clean &= self
                    .acl(path, REVOKE_ACCESS, 0, SUB_CONTAINERS_AND_OBJECTS_INHERIT)
                    .is_ok();
            }
        }
        unsafe {
            FreeSid(self.sid);
            clean &= DeleteAppContainerProfile(self.name.as_ptr()) >= 0;
        }
        let _ = std::fs::remove_dir_all(&self.temp);
        if clean {
            if let Some(marker) = &self.marker {
                let _ = std::fs::remove_file(marker);
            }
        }
    }
}

struct Attributes {
    storage: Vec<usize>,
}
impl Attributes {
    fn new() -> Result<Self, String> {
        let mut bytes = 0;
        unsafe {
            InitializeProcThreadAttributeList(null_mut(), 2, 0, &mut bytes);
        }
        let mut attributes = Self {
            storage: vec![0; bytes.div_ceil(std::mem::size_of::<usize>())],
        };
        if unsafe { InitializeProcThreadAttributeList(attributes.ptr(), 2, 0, &mut bytes) } == 0 {
            return Err(failure("Initialize process attributes"));
        }
        Ok(attributes)
    }
    fn ptr(&mut self) -> LPPROC_THREAD_ATTRIBUTE_LIST {
        self.storage.as_mut_ptr().cast()
    }
    fn set<T>(&mut self, attribute: u32, value: &[T]) -> Result<(), String> {
        if unsafe {
            UpdateProcThreadAttribute(
                self.ptr(),
                0,
                attribute as usize,
                value.as_ptr().cast(),
                std::mem::size_of_val(value),
                null_mut(),
                null(),
            )
        } == 0
        {
            return Err(failure("Set process security attributes"));
        }
        Ok(())
    }
}
impl Drop for Attributes {
    fn drop(&mut self) {
        unsafe {
            DeleteProcThreadAttributeList(self.ptr());
        }
    }
}

fn pipe(parent_reads: bool) -> Result<(File, OwnedHandle), String> {
    let security = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: null_mut(),
        bInheritHandle: 1,
    };
    let (mut read, mut write) = (null_mut(), null_mut());
    if unsafe { CreatePipe(&mut read, &mut write, &security, 0) } == 0 {
        return Err(failure("Create process pipe"));
    }
    let (parent, child) = if parent_reads {
        (read, write)
    } else {
        (write, read)
    };
    let parent = unsafe { File::from_raw_handle(parent) };
    let child = unsafe { OwnedHandle::from_raw_handle(child) };
    if unsafe { SetHandleInformation(parent.as_raw_handle(), HANDLE_FLAG_INHERIT, 0) } == 0 {
        return Err(failure("Constrain inherited handles"));
    }
    Ok((parent, child))
}

pub struct Process {
    handle: OwnedHandle,
    thread: OwnedHandle,
    _profile: Profile,
    _project_drive: ProjectDrive,
    pub stdin: Option<File>,
    pub stdout: Option<File>,
    pub stderr: Option<File>,
}
impl AsRawHandle for Process {
    fn as_raw_handle(&self) -> std::os::windows::io::RawHandle {
        self.handle.as_raw_handle()
    }
}
impl Process {
    pub fn suspended(
        shell: &Path,
        bootstrap: &str,
        root: &Path,
        protected: &Path,
        cwd: &Path,
    ) -> Result<Self, String> {
        let mut profile = Profile::create(root, protected)?;
        let project_drive = ProjectDrive::new(root)?;
        let relative_cwd = cwd
            .strip_prefix(root)
            .map_err(|_| "terminal_isolation_unavailable: Working directory escaped project")?;
        let working_directory = project_drive.path.join(relative_cwd);
        let (stdin, input) = pipe(false)?;
        let (stdout, output) = pipe(true)?;
        let (stderr, error) = pipe(true)?;
        let mut attributes = Attributes::new()?;
        let security = SECURITY_CAPABILITIES {
            AppContainerSid: profile.sid,
            Capabilities: null_mut(),
            CapabilityCount: 0,
            Reserved: 0,
        };
        let handles = [
            input.as_raw_handle(),
            output.as_raw_handle(),
            error.as_raw_handle(),
        ];
        attributes.set(
            PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            std::slice::from_ref(&security),
        )?;
        attributes.set(PROC_THREAD_ATTRIBUTE_HANDLE_LIST, &handles)?;
        let mut startup: STARTUPINFOEXW = unsafe { std::mem::zeroed() };
        startup.StartupInfo.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = input.as_raw_handle();
        startup.StartupInfo.hStdOutput = output.as_raw_handle();
        startup.StartupInfo.hStdError = error.as_raw_handle();
        startup.lpAttributeList = attributes.ptr();
        // Fixed bootstrap only. Model-authored command is supplied through stdin after resume.
        if bootstrap.contains('"') {
            return Err("terminal_isolation_unavailable: Invalid trusted bootstrap".into());
        }
        // Load only Windows PowerShell's trusted modules. AppContainer registry
        // discovery and module caches can differ from the parent shell.
        let module_bootstrap = "try { Import-Module ($PSHOME+'\\Modules\\Microsoft.PowerShell.Management') -ErrorAction Stop; Import-Module ($PSHOME+'\\Modules\\Microsoft.PowerShell.Utility') -ErrorAction Stop } catch { [Console]::Error.WriteLine(('terminal_module_load_failed: '+$_.ToString())); exit 125 };";
        let mut command = wide(format!("\"{}\" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -OutputFormat Text -Command \"{module_bootstrap}{bootstrap}\"", shell.display()));
        let system = crate::terminal_executor::system_directory()?;
        let windows = system
            .parent()
            .ok_or("terminal_isolation_unavailable: Invalid Windows path")?;
        let mut entries = vec![
            format!(
                "PATH={};{}",
                system.display(),
                shell.parent().unwrap().display()
            ),
            "PATHEXT=.COM;.EXE;.BAT;.CMD".into(),
            format!(
                "PSModulePath={}",
                shell.parent().unwrap().join("Modules").display()
            ),
            format!("AGENT_TERMINAL_CWD={}", working_directory.display()),
            format!("SystemRoot={}", windows.display()),
            format!("WINDIR={}", windows.display()),
            format!("TEMP={}", profile.temp.display()),
            format!("TMP={}", profile.temp.display()),
        ];
        for key in [
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
            "SystemDrive",
            "COMSPEC",
            "ProgramData",
            "ProgramFiles",
            "ProgramFiles(x86)",
        ] {
            if let Some(value) = std::env::var_os(key) {
                entries.push(format!("{key}={}", value.to_string_lossy()));
            }
        }
        entries.sort_by_key(|entry| entry.to_lowercase());
        let mut environment = Vec::<u16>::new();
        for entry in entries {
            environment.extend(wide(entry));
        }
        environment.push(0);
        let mut info: PROCESS_INFORMATION = unsafe { std::mem::zeroed() };
        let success = unsafe {
            CreateProcessW(
                wide(shell).as_ptr(),
                command.as_mut_ptr(),
                null(),
                null(),
                1,
                CREATE_SUSPENDED
                    | CREATE_NO_WINDOW
                    | EXTENDED_STARTUPINFO_PRESENT
                    | CREATE_UNICODE_ENVIRONMENT,
                environment.as_ptr().cast(),
                wide(&system).as_ptr(),
                &startup.StartupInfo,
                &mut info,
            )
        };
        if success == 0 {
            return Err(failure("Start capability-free AppContainer"));
        }
        profile.record.owner_pid = info.dwProcessId;
        profile.record.owner_birth =
            process_birth(info.hProcess).unwrap_or(profile.record.owner_birth);
        let process = Self {
            handle: unsafe { OwnedHandle::from_raw_handle(info.hProcess) },
            thread: unsafe { OwnedHandle::from_raw_handle(info.hThread) },
            _profile: profile,
            _project_drive: project_drive,
            stdin: Some(stdin),
            stdout: Some(stdout),
            stderr: Some(stderr),
        };
        process.verify_token()?;
        process._profile.persist()?;
        Ok(process)
    }
    fn verify_token(&self) -> Result<(), String> {
        unsafe {
            let mut token = null_mut();
            if OpenProcessToken(self.handle.as_raw_handle(), TOKEN_QUERY, &mut token) == 0 {
                return Err(failure("Inspect isolated token"));
            }
            let token = OwnedHandle::from_raw_handle(token);
            let mut bytes = 0;
            let mut isolated = 0u32;
            if GetTokenInformation(
                token.as_raw_handle(),
                TokenIsAppContainer,
                (&mut isolated as *mut u32).cast(),
                4,
                &mut bytes,
            ) == 0
                || isolated != 1
            {
                return Err(
                    "terminal_isolation_unavailable: Child token is not AppContainer".into(),
                );
            }
            let mut data = vec![0usize; 128];
            if GetTokenInformation(
                token.as_raw_handle(),
                TokenAppContainerSid,
                data.as_mut_ptr().cast(),
                std::mem::size_of_val(data.as_slice()) as u32,
                &mut bytes,
            ) == 0
            {
                return Err(failure("Inspect isolated profile identity"));
            }
            let identity = &*data.as_ptr().cast::<TOKEN_APPCONTAINER_INFORMATION>();
            if EqualSid(identity.TokenAppContainer, self._profile.sid) == 0 {
                return Err(
                    "terminal_isolation_unavailable: Child profile identity mismatch".into(),
                );
            }
            if GetTokenInformation(
                token.as_raw_handle(),
                TokenCapabilities,
                data.as_mut_ptr().cast(),
                std::mem::size_of_val(data.as_slice()) as u32,
                &mut bytes,
            ) == 0
                || (*data.as_ptr().cast::<TOKEN_GROUPS>()).GroupCount != 0
            {
                return Err(
                    "terminal_isolation_unavailable: Child token has unexpected capabilities"
                        .into(),
                );
            }
        }
        Ok(())
    }
    pub fn resume(&self) -> Result<(), String> {
        if unsafe { ResumeThread(self.thread.as_raw_handle()) } == u32::MAX {
            return Err(failure("Resume isolated process"));
        }
        Ok(())
    }
    pub fn try_wait(&mut self) -> std::io::Result<Option<std::process::ExitStatus>> {
        use std::os::windows::process::ExitStatusExt;
        match unsafe { WaitForSingleObject(self.handle.as_raw_handle(), 0) } {
            WAIT_TIMEOUT => Ok(None),
            WAIT_OBJECT_0 => {
                let mut code = 0;
                if unsafe { GetExitCodeProcess(self.handle.as_raw_handle(), &mut code) } == 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(Some(std::process::ExitStatus::from_raw(code)))
            }
            _ => Err(std::io::Error::last_os_error()),
        }
    }
    pub fn kill(&mut self) -> std::io::Result<()> {
        if unsafe { TerminateProcess(self.handle.as_raw_handle(), 125) } == 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(())
    }
    pub fn wait(&mut self) -> std::io::Result<()> {
        if unsafe { WaitForSingleObject(self.handle.as_raw_handle(), INFINITE) } != WAIT_OBJECT_0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(())
    }
}
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.kill();
        let _ = self.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;
    #[test]
    fn project_hardlink_to_external_file_is_rejected_before_grant() {
        let base =
            std::env::temp_dir().join(format!("agent-terminal-link-{}", uuid::Uuid::new_v4()));
        let root = base.join("project");
        std::fs::create_dir_all(&root).unwrap();
        let outside = base.join("private.txt");
        std::fs::write(&outside, "private").unwrap();
        std::fs::hard_link(&outside, root.join("alias.txt")).unwrap();
        let result = Profile::create(&root, Path::new(""));
        let rejected = result.is_err();
        drop(result);
        std::fs::remove_dir_all(base).unwrap();
        assert!(rejected, "External hardlink received terminal authority");
    }
    #[test]
    fn alias_added_after_grant_does_not_inherit_terminal_authority() {
        let base =
            std::env::temp_dir().join(format!("agent-terminal-late-link-{}", uuid::Uuid::new_v4()));
        let root = base.join("project");
        std::fs::create_dir_all(&root).unwrap();
        let outside = base.join("private.txt");
        std::fs::write(&outside, "private").unwrap();
        let mut process = Process::suspended(
            &crate::terminal_executor::powershell_path().unwrap(),
            r#"try { [IO.File]::WriteAllText(($env:AGENT_TERMINAL_CWD+'\alias.txt'),'changed'); exit 1 } catch { exit 0 }"#,
            &root, Path::new(""), &root,
        ).unwrap();
        std::fs::hard_link(&outside, root.join("alias.txt")).unwrap();
        process.resume().unwrap();
        process.wait().unwrap();
        assert!(process.try_wait().unwrap().unwrap().success());
        drop(process);
        assert_eq!(std::fs::read_to_string(&outside).unwrap(), "private");
        std::fs::remove_dir_all(base).unwrap();
    }
    #[test]
    fn isolated_process_reads_project_through_temporary_drive() {
        let root =
            std::env::temp_dir().join(format!("agent-terminal-probe-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        std::fs::write(root.join("read.txt"), "readable").unwrap();
        let bootstrap = r#"[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false); [Console]::WriteLine(('modules='+$env:PSModulePath)); [Console]::WriteLine(('cwd='+$env:AGENT_TERMINAL_CWD)); try { [Console]::WriteLine([IO.File]::ReadAllText(($env:AGENT_TERMINAL_CWD+'\read.txt'))) } catch { [Console]::WriteLine(('read='+$_.Exception.ToString())) }; try { Set-Location -LiteralPath $env:AGENT_TERMINAL_CWD -ErrorAction Stop; [Console]::WriteLine('location-ok') } catch { [Console]::WriteLine(('location='+$_.Exception.ToString())) }; exit 0"#;
        let mut process = Process::suspended(
            &crate::terminal_executor::powershell_path().unwrap(),
            bootstrap,
            &root,
            Path::new(""),
            &root,
        )
        .unwrap();
        process.resume().unwrap();
        let mut output = Vec::new();
        process
            .stdout
            .take()
            .unwrap()
            .read_to_end(&mut output)
            .unwrap();
        let output = String::from_utf8_lossy(&output).into_owned();
        process.wait().unwrap();
        println!("{output}");
        drop(process);
        std::fs::remove_dir_all(root).unwrap();
        assert!(output.contains("location-ok"), "{output}");
        let modules = crate::terminal_executor::powershell_path()
            .unwrap()
            .parent()
            .unwrap()
            .join("Modules");
        assert!(
            output
                .lines()
                .any(|line| line == format!("modules={}", modules.display())),
            "{output}"
        );
    }
    #[test]
    fn abandoned_profile_grants_are_recovered_without_removing_project_files() {
        let root =
            std::env::temp_dir().join(format!("agent-terminal-recovery-{}", uuid::Uuid::new_v4()));
        let private =
            std::env::temp_dir().join(format!("agent-terminal-private-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        std::fs::create_dir(&private).unwrap();
        std::fs::write(root.join("keep.txt"), "keep").unwrap();
        let mut profile = Profile::create(&root, &private).unwrap();
        let marker = profile.marker.clone().unwrap();
        assert!(marker.exists());
        profile.record.owner_pid = u32::MAX;
        profile.record.owner_birth = 0;
        profile.persist().unwrap();
        Profile::recover(&private.join("terminal-isolation")).unwrap();
        assert!(!marker.exists());
        assert!(!profile.temp.exists());
        assert_eq!(
            std::fs::read_to_string(root.join("keep.txt")).unwrap(),
            "keep"
        );
        drop(profile);
        std::fs::remove_dir_all(root).unwrap();
        std::fs::remove_dir_all(private).unwrap();
    }
}
