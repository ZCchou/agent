//! Host-owned file access capabilities. Paths from tools are never filesystem authority.
use sha2::{Digest, Sha256};
use std::{
    ffi::OsStr,
    fs::File,
    io::{Read, Seek, SeekFrom, Write},
    path::{Component, Path, PathBuf},
};

#[cfg(windows)]
#[path = "file_access/windows.rs"]
mod windows;
#[cfg(windows)]
use windows as platform;
#[cfg(not(windows))]
#[path = "file_access/unsupported.rs"]
mod platform;

pub const POLICY_VERSION: u32 = 1;
pub type FileAccessError = String;

/// Constructed only by the Host, never deserialized from tool or WebView input.
#[derive(Debug, Clone)]
pub struct AuthorizedRoot {
    // Scope binding fields are consumed by the H2 search lifecycle, not by path parsing.
    #[allow(dead_code)]
    pub project_id: String,
    #[allow(dead_code)]
    pub conversation_id: String,
    #[allow(dead_code)]
    pub root_generation: u64,
    pub policy_version: u32,
    pub root: PathBuf,
    pub protected_root: PathBuf,
}

impl AuthorizedRoot {
    pub fn for_host_path(root: &Path, protected_root: &Path) -> Self {
        Self {
            project_id: String::new(),
            conversation_id: String::new(),
            root_generation: 0,
            policy_version: POLICY_VERSION,
            root: root.to_path_buf(),
            protected_root: protected_root.to_path_buf(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RelativePath(PathBuf);
impl RelativePath {
    pub fn as_path(&self) -> &Path {
        &self.0
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct FileIdentity {
    pub volume: u64,
    pub file: u128,
}

#[derive(Debug)]
struct HandleInfo {
    identity: FileIdentity,
    path: PathBuf,
    is_dir: bool,
    len: u64,
}

/// Retains every ancestor handle until the actual operation has finished.
pub struct VerifiedParentHandle {
    chain: Vec<File>,
    path: PathBuf,
}
impl VerifiedParentHandle {
    pub fn identity(&self) -> Result<FileIdentity, FileAccessError> {
        self.verify()?;
        Ok(platform::inspect(self.directory())?.identity)
    }
    fn directory(&self) -> &File {
        self.chain.last().expect("verified parent has a directory")
    }
    fn verify(&self) -> Result<(), FileAccessError> {
        for file in &self.chain {
            platform::inspect(file)?;
        }
        checked_info(self.directory(), &self.path)?;
        Ok(())
    }
}

pub struct VerifiedReadHandle {
    file: File,
    _ancestors: Vec<File>,
    info: HandleInfo,
}
impl VerifiedReadHandle {
    pub fn identity(&self) -> FileIdentity {
        self.info.identity
    }
    pub fn path(&self) -> &Path {
        &self.info.path
    }
    pub fn is_dir(&self) -> bool {
        self.info.is_dir
    }
    pub fn is_file(&self) -> bool {
        !self.info.is_dir
    }
    pub fn len(&self) -> u64 {
        self.info.len
    }
    pub fn read_bytes(&self, maximum: u64) -> Result<Vec<u8>, FileAccessError> {
        if self.is_dir() || self.len() > maximum {
            return Err(error("tool_limit_exceeded"));
        }
        let mut file = &self.file;
        file.seek(SeekFrom::Start(0))
            .map_err(|_| error("file_read_failed"))?;
        let mut bytes = Vec::new();
        file.take(maximum.saturating_add(1))
            .read_to_end(&mut bytes)
            .map_err(|_| error("file_read_failed"))?;
        if bytes.len() as u64 > maximum {
            return Err(error("tool_limit_exceeded"));
        }
        // The very handle used for content is also the identity/hash authority.
        if platform::inspect(&self.file)?.identity != self.identity() {
            return Err(error("file_identity_changed"));
        }
        Ok(bytes)
    }
    pub fn hash(&self, maximum: u64) -> Result<String, FileAccessError> {
        Ok(format!("{:x}", Sha256::digest(self.read_bytes(maximum)?)))
    }
    pub fn read_chunk(&self, buffer: &mut [u8]) -> Result<usize, FileAccessError> {
        (&self.file)
            .read(buffer)
            .map_err(|_| error("file_read_failed"))
    }
    pub fn visit_entries(
        &self,
        visitor: &mut dyn FnMut(String) -> bool,
    ) -> Result<(), FileAccessError> {
        if !self.is_dir() {
            return Err(error("not_a_directory"));
        }
        platform::visit_entries(&self.file, visitor)
    }
    pub fn entries(&self) -> Result<Vec<String>, FileAccessError> {
        if !self.is_dir() {
            return Err(error("not_a_directory"));
        }
        let mut entries = Vec::new();
        self.visit_entries(&mut |name| {
            entries.push(name);
            true
        })?;
        Ok(entries)
    }
}

pub struct FileAccessPolicy;
impl FileAccessPolicy {
    pub fn validate_relative(&self, path: &str) -> Result<RelativePath, FileAccessError> {
        let path = path.replace('\\', "/");
        if path.is_empty() || path.starts_with('/') {
            return Err(error("forbidden_path"));
        }
        let mut result = PathBuf::new();
        for (index, component) in path.split('/').enumerate() {
            if component.is_empty() || (component == "." && index == 0) {
                continue;
            }
            if component == "."
                || component == ".."
                || component.ends_with(['.', ' '])
                || component
                    .chars()
                    .any(|c| c < ' ' || matches!(c, ':' | '?' | '*' | '"' | '<' | '>' | '|' | '~'))
                || sensitive_component(component)
                || device_component(component)
            {
                return Err(error("forbidden_path"));
            }
            result.push(component);
        }
        Ok(RelativePath(result))
    }

    fn check_path(&self, root: &AuthorizedRoot, path: &Path) -> Result<(), FileAccessError> {
        if root.policy_version != POLICY_VERSION || !inside(&root.root, path) {
            return Err(error("path_escape"));
        }
        if !root.protected_root.as_os_str().is_empty() && inside(&root.protected_root, path) {
            return Err(error("protected_path"));
        }
        Ok(())
    }

    fn pin_root(&self, root: &AuthorizedRoot) -> Result<VerifiedParentHandle, FileAccessError> {
        self.check_path(root, &root.root)?;
        let (chain, path) = platform::pin_absolute(&root.root)?;
        if !same_path(&path, &root.root) {
            return Err(error("path_escape"));
        }
        Ok(VerifiedParentHandle { chain, path })
    }

    fn pin_directory(
        &self,
        root: &AuthorizedRoot,
        relative: &Path,
    ) -> Result<VerifiedParentHandle, FileAccessError> {
        let mut parent = self.pin_root(root)?;
        for component in relative.components() {
            let Component::Normal(name) = component else {
                return Err(error("invalid_path"));
            };
            let path = parent.path.join(name);
            self.check_path(root, &path)?;
            let file = platform::open_child(parent.directory(), name, false, false, true)?;
            let info = checked_info(&file, &path)?;
            if !info.is_dir {
                return Err(error("not_a_directory"));
            }
            parent.chain.push(file);
            parent.path = info.path;
        }
        Ok(parent)
    }

    pub fn open_parent(
        &self,
        root: &AuthorizedRoot,
        path: &RelativePath,
    ) -> Result<VerifiedParentHandle, FileAccessError> {
        if path.0.as_os_str().is_empty() {
            return Err(error("forbidden_path"));
        }
        self.check_path(root, &root.root.join(&path.0))?;
        self.pin_directory(root, path.0.parent().unwrap_or(Path::new("")))
    }

    pub fn open_read(
        &self,
        root: &AuthorizedRoot,
        path: &RelativePath,
    ) -> Result<VerifiedReadHandle, FileAccessError> {
        #[cfg(test)]
        tests::hook(tests::Stage::AfterValidate);
        let mut parent = if path.0.as_os_str().is_empty() {
            self.pin_root(root)?
        } else {
            self.open_parent(root, path)?
        };
        let (file, expected) = if let Some(name) = path.0.file_name() {
            (
                platform::open_child(parent.directory(), name, false, false, false)?,
                parent.path.join(name),
            )
        } else {
            (
                parent.chain.pop().expect("root is pinned"),
                parent.path.clone(),
            )
        };
        let info = checked_info(&file, &expected)?;
        self.check_path(root, &info.path)?;
        #[cfg(test)]
        tests::hook(tests::Stage::AfterOpen);
        for ancestor in &parent.chain {
            platform::inspect(ancestor)?;
        }
        Ok(VerifiedReadHandle {
            file,
            _ancestors: parent.chain,
            info,
        })
    }

    /// Validate every suffix and its closest existing ancestor without creating it.
    pub(crate) fn validate_planned_target(
        &self,
        root: &AuthorizedRoot,
        path: &RelativePath,
    ) -> Result<(), FileAccessError> {
        self.check_path(root, &root.root.join(path.as_path()))?;
        let mut ancestor = path.as_path().parent();
        while let Some(parent) = ancestor {
            match self.open_read(root, &RelativePath(parent.to_path_buf())) {
                Ok(file) if file.is_dir() => return Ok(()),
                Ok(_) => return Err(error("not_a_directory")),
                Err(e) if e.starts_with("path_not_found:") => {
                    self.check_path(root, &root.root.join(parent))?;
                }
                Err(e) => return Err(e),
            }
            ancestor = parent.parent();
        }
        self.open_read(root, &RelativePath(PathBuf::new()))?;
        Ok(())
    }

    #[cfg(test)]
    pub fn target_path(
        &self,
        root: &AuthorizedRoot,
        path: &RelativePath,
    ) -> Result<PathBuf, FileAccessError> {
        // This is for display/planning only. It is never authority for subsequent I/O.
        if path.0.as_os_str().is_empty() {
            return Ok(self.open_read(root, path)?.path().to_path_buf());
        }
        let parent = self.open_parent(root, path)?;
        let name = path.0.file_name().ok_or_else(|| error("invalid_path"))?;
        match platform::open_child(parent.directory(), name, false, false, false) {
            Ok(file) => {
                checked_info(&file, &parent.path.join(name))?;
            }
            Err(error) if error.starts_with("path_not_found:") => {}
            Err(error) => return Err(error),
        }
        Ok(parent.path.join(name))
    }

    #[cfg(test)]
    pub fn write(
        &self,
        root: &AuthorizedRoot,
        path: &RelativePath,
        bytes: &[u8],
        create_only: bool,
        expected: Option<&str>,
    ) -> Result<(), FileAccessError> {
        self.write_checked(
            root,
            path,
            bytes,
            create_only,
            expected,
            None,
            &uuid::Uuid::new_v4().to_string(),
        )
        .map(|_| ())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn write_checked(
        &self,
        root: &AuthorizedRoot,
        path: &RelativePath,
        bytes: &[u8],
        create_only: bool,
        expected: Option<&str>,
        identity: Option<FileIdentity>,
        artifact_id: &str,
    ) -> Result<FileIdentity, FileAccessError> {
        let parent = self.open_parent(root, path)?;
        let name = path.0.file_name().ok_or_else(|| error("invalid_path"))?;
        let existing = self.open_mutation(&parent, name)?;
        if create_only && existing.is_some() {
            return Err(error("file_already_exists"));
        }
        check_identity(existing.as_ref(), identity)?;
        check_expected(existing.as_ref(), expected)?;
        if existing.as_ref().is_some_and(VerifiedReadHandle::is_dir) {
            return Err(error("unsupported_file_type"));
        }
        #[cfg(test)]
        tests::hook(tests::Stage::BeforeCreate);
        parent.verify()?;
        let temporary_name = format!(".agent-{artifact_id}.tmp");
        let mut temporary = platform::open_child(
            parent.directory(),
            OsStr::new(&temporary_name),
            true,
            true,
            false,
        )?;
        checked_info(&temporary, &parent.path.join(&temporary_name))?;
        if temporary
            .write_all(bytes)
            .and_then(|_| temporary.sync_all())
            .is_err()
        {
            let _ = platform::delete(&temporary);
            return Err(error("file_write_failed"));
        }
        #[cfg(test)]
        crate::change_set_recovery::tests::checkpoint("write_staged")?;
        let backup_name = format!(".agent-{artifact_id}.bak");
        if let Some(existing) = &existing {
            if let Err(error) =
                platform::rename(&existing.file, parent.directory(), OsStr::new(&backup_name))
            {
                let _ = platform::delete(&temporary);
                return Err(error);
            }
            #[cfg(test)]
            crate::change_set_recovery::tests::checkpoint("write_backed_up")?;
        }
        if let Err(error) = platform::rename(&temporary, parent.directory(), name) {
            let _ = platform::delete(&temporary);
            if let Some(existing) = &existing {
                if platform::rename(&existing.file, parent.directory(), name).is_err() {
                    return Err(self::error("recovery_required"));
                }
            }
            return Err(error);
        }
        #[cfg(test)]
        crate::change_set_recovery::tests::checkpoint("write_replaced")?;
        if let Some(existing) = existing {
            platform::delete(&existing.file)?;
        }
        temporary
            .sync_all()
            .map_err(|_| error("file_flush_failed"))?;
        let info = checked_info(&temporary, &parent.path.join(name))?;
        let written = VerifiedReadHandle {
            file: temporary,
            _ancestors: Vec::new(),
            info,
        };
        if written.hash(bytes.len() as u64)? != format!("{:x}", Sha256::digest(bytes)) {
            return Err(error("file_write_verification_failed"));
        }
        Ok(written.identity())
    }

    fn open_mutation(
        &self,
        parent: &VerifiedParentHandle,
        name: &OsStr,
    ) -> Result<Option<VerifiedReadHandle>, FileAccessError> {
        match platform::open_child(parent.directory(), name, true, false, false) {
            Ok(file) => {
                let info = checked_info(&file, &parent.path.join(name))?;
                Ok(Some(VerifiedReadHandle {
                    file,
                    _ancestors: Vec::new(),
                    info,
                }))
            }
            Err(error) if error.starts_with("path_not_found:") => Ok(None),
            Err(error) => Err(error),
        }
    }

    #[cfg(test)]
    pub fn create_directory(
        &self,
        root: &AuthorizedRoot,
        path: &RelativePath,
    ) -> Result<(), FileAccessError> {
        self.create_directory_checked(root, path).map(|_| ())
    }

    pub fn create_directory_checked(
        &self,
        root: &AuthorizedRoot,
        path: &RelativePath,
    ) -> Result<FileIdentity, FileAccessError> {
        let parent = self.open_parent(root, path)?;
        let name = path.0.file_name().ok_or_else(|| error("invalid_path"))?;
        #[cfg(test)]
        tests::hook(tests::Stage::BeforeCreate);
        parent.verify()?;
        let file = platform::open_child(parent.directory(), name, true, true, true)?;
        Ok(checked_info(&file, &parent.path.join(name))?.identity)
    }

    #[cfg(test)]
    pub fn delete(
        &self,
        root: &AuthorizedRoot,
        path: &RelativePath,
        expected: Option<&str>,
    ) -> Result<(), FileAccessError> {
        self.delete_checked(root, path, expected, None)
    }

    pub fn delete_checked(
        &self,
        root: &AuthorizedRoot,
        path: &RelativePath,
        expected: Option<&str>,
        identity: Option<FileIdentity>,
    ) -> Result<(), FileAccessError> {
        let parent = self.open_parent(root, path)?;
        let file = self
            .open_mutation(
                &parent,
                path.0.file_name().ok_or_else(|| error("invalid_path"))?,
            )?
            .ok_or_else(|| error("path_not_found"))?;
        check_identity(Some(&file), identity)?;
        check_expected(Some(&file), expected)?;
        platform::delete(&file.file)
    }

    #[cfg(test)]
    pub fn move_file(
        &self,
        root: &AuthorizedRoot,
        source: &RelativePath,
        target: &RelativePath,
        expected: Option<&str>,
    ) -> Result<(), FileAccessError> {
        self.move_file_checked(root, source, target, expected, None)
            .map(|_| ())
    }

    pub fn move_file_checked(
        &self,
        root: &AuthorizedRoot,
        source: &RelativePath,
        target: &RelativePath,
        expected: Option<&str>,
        identity: Option<FileIdentity>,
    ) -> Result<FileIdentity, FileAccessError> {
        let source_parent = self.open_parent(root, source)?;
        let target_parent = self.open_parent(root, target)?;
        let source_name = source.0.file_name().ok_or_else(|| error("invalid_path"))?;
        let file = self
            .open_mutation(&source_parent, source_name)?
            .ok_or_else(|| error("path_not_found"))?;
        if file.is_dir() {
            return Err(error("unsupported_file_type"));
        }
        check_identity(Some(&file), identity)?;
        check_expected(Some(&file), expected)?;
        platform::rename(
            &file.file,
            target_parent.directory(),
            target.0.file_name().ok_or_else(|| error("invalid_path"))?,
        )?;
        Ok(file.identity())
    }
}

fn check_identity(
    file: Option<&VerifiedReadHandle>,
    expected: Option<FileIdentity>,
) -> Result<(), FileAccessError> {
    if expected.is_some_and(|identity| file.map(VerifiedReadHandle::identity) != Some(identity)) {
        return Err(error("file_identity_changed"));
    }
    Ok(())
}

fn check_expected(
    file: Option<&VerifiedReadHandle>,
    expected: Option<&str>,
) -> Result<(), FileAccessError> {
    if let Some(expected) = expected {
        if file
            .map(|file| file.hash(20 * 1024 * 1024))
            .transpose()?
            .as_deref()
            != Some(expected)
        {
            return Err(error("file_changed_since_read"));
        }
    }
    Ok(())
}

fn checked_info(file: &File, expected: &Path) -> Result<HandleInfo, FileAccessError> {
    let info = platform::inspect(file)?;
    if !same_path(&info.path, expected) {
        return Err(error("path_escape"));
    }
    Ok(info)
}
// Compare identity without ever rewriting the spelling used for filesystem I/O.
#[cfg(windows)]
pub(crate) fn windows_path_eq(a: &str, b: &str) -> bool {
    #[link(name = "kernel32")]
    extern "system" {
        fn CompareStringOrdinal(
            a: *const u16,
            a_len: i32,
            b: *const u16,
            b_len: i32,
            ignore_case: i32,
        ) -> i32;
    }
    let a: Vec<u16> = a.encode_utf16().collect();
    let b: Vec<u16> = b.encode_utf16().collect();
    // Validated local paths fit i32; API failure is unequal (fail closed).
    let (Ok(a_len), Ok(b_len)) = (i32::try_from(a.len()), i32::try_from(b.len())) else {
        return false;
    };
    unsafe { CompareStringOrdinal(a.as_ptr(), a_len, b.as_ptr(), b_len, 1) == 2 }
}
#[cfg(not(windows))]
pub(crate) fn windows_path_eq(a: &str, b: &str) -> bool {
    a.eq_ignore_ascii_case(b)
}

fn normalized(path: &Path) -> String {
    path.to_string_lossy()
        .replace('/', "\\")
        .trim_start_matches(r"\\?\")
        .trim_end_matches('\\')
        .to_string()
}
fn same_path(a: &Path, b: &Path) -> bool {
    windows_path_eq(&normalized(a), &normalized(b))
}
fn inside(root: &Path, path: &Path) -> bool {
    let root = normalized(root);
    let path = normalized(path);
    windows_path_eq(&path, &root)
        || path
            .match_indices('\\')
            .any(|(end, _)| windows_path_eq(&path[..end], &root))
}
fn error(code: &str) -> FileAccessError {
    format!("{code}: The file access policy could not verify this operation")
}
fn sensitive_component(component: &str) -> bool {
    let c = component.to_ascii_lowercase();
    c == ".git"
        || c == ".env"
        || c.starts_with(".env.")
        || c.contains("credential")
        || c.contains("secret")
        || matches!(
            c.as_str(),
            ".ssh"
                | ".aws"
                | ".azure"
                | ".gnupg"
                | ".kube"
                | ".netrc"
                | ".npmrc"
                | ".pypirc"
                | "id_rsa"
                | "id_ed25519"
        )
        || [".pem", ".key", ".p12", ".pfx"]
            .iter()
            .any(|suffix| c.ends_with(suffix))
}
fn device_component(component: &str) -> bool {
    let stem = component
        .split('.')
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    matches!(
        stem.as_str(),
        "con" | "prn" | "aux" | "nul" | "conin$" | "conout$"
    ) || ["com", "lpt"].iter().any(|prefix| {
        stem.strip_prefix(prefix).is_some_and(|n| {
            matches!(
                n,
                "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"
            )
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_comparison_does_not_merge_expanding_unicode_names() {
        assert!(same_path(
            Path::new("C:/Root/README"),
            Path::new("c:/root/readme")
        ));
        assert!(!same_path(
            Path::new("C:/Root/İ"),
            Path::new("C:/Root/i\u{0307}")
        ));
        assert!(!inside(
            Path::new("C:/Root/İ"),
            Path::new("C:/Root/i\u{0307}/file")
        ));
        assert!(inside(Path::new("C:/Root/İ"), Path::new("c:/root/İ/file")));
        assert!(!inside(
            Path::new("C:/Root/a"),
            Path::new("C:/Root/another/file")
        ));
    }
    use std::{cell::RefCell, fs};
    #[derive(Clone, Copy, PartialEq, Eq)]
    pub(super) enum Stage {
        AfterValidate,
        AfterOpen,
        BeforeCreate,
        BeforeNativeCreate,
    }
    type Hook = Box<dyn FnMut(Stage)>;
    thread_local! { static HOOK: RefCell<Option<Hook>> = RefCell::new(None); }
    pub(super) fn hook(stage: Stage) {
        HOOK.with(|cell| {
            if let Some(hook) = cell.borrow_mut().as_mut() {
                hook(stage);
            }
        });
    }
    fn inject(stage: Stage, action: impl FnOnce() + 'static) {
        let mut action = Some(action);
        HOOK.with(|cell| {
            *cell.borrow_mut() = Some(Box::new(move |at| {
                if at == stage {
                    if let Some(action) = action.take() {
                        action();
                    }
                }
            }))
        });
    }
    struct Fixture {
        base: PathBuf,
        root: AuthorizedRoot,
    }
    impl Fixture {
        fn new() -> Self {
            let base = std::env::temp_dir().join(format!("agent-access-{}", uuid::Uuid::new_v4()));
            fs::create_dir_all(base.join("project/nested")).unwrap();
            fs::create_dir_all(base.join("outside")).unwrap();
            let base = base.canonicalize().unwrap();
            let root =
                AuthorizedRoot::for_host_path(&base.join("project"), &base.join("project/state"));
            Self { base, root }
        }
        fn relative(&self, value: &str) -> RelativePath {
            FileAccessPolicy.validate_relative(value).unwrap()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            HOOK.with(|cell| *cell.borrow_mut() = None);
            fs::remove_dir_all(&self.base).unwrap();
        }
    }
    #[cfg(windows)]
    fn junction(path: &Path, target: &Path) {
        let result = std::process::Command::new("cmd")
            .args(["/C", "mklink", "/J"])
            .arg(path)
            .arg(target)
            .output()
            .unwrap();
        assert!(result.status.success());
    }

    #[cfg(windows)]
    fn set_junction_in_place(path: &Path, target: &Path) {
        use std::os::windows::{fs::OpenOptionsExt, io::AsRawHandle};
        use windows_sys::Win32::{
            Storage::FileSystem::{FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT},
            System::IO::DeviceIoControl,
        };
        let substitute = format!(
            r"\??\{}",
            target.to_string_lossy().trim_start_matches(r"\\?\")
        );
        let substitute: Vec<u16> = substitute.encode_utf16().collect();
        let print: Vec<u16> = target.to_string_lossy().encode_utf16().collect();
        let data_len = 8 + (substitute.len() + print.len() + 2) * 2;
        let mut data = Vec::new();
        data.extend_from_slice(&0xa0000003_u32.to_le_bytes());
        data.extend_from_slice(&(data_len as u16).to_le_bytes());
        data.extend_from_slice(&0_u16.to_le_bytes());
        data.extend_from_slice(&0_u16.to_le_bytes());
        data.extend_from_slice(&((substitute.len() * 2) as u16).to_le_bytes());
        data.extend_from_slice(&(((substitute.len() + 1) * 2) as u16).to_le_bytes());
        data.extend_from_slice(&((print.len() * 2) as u16).to_le_bytes());
        for word in substitute.into_iter().chain([0]).chain(print).chain([0]) {
            data.extend_from_slice(&word.to_le_bytes());
        }
        let file = fs::OpenOptions::new()
            .write(true)
            .share_mode(7)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)
            .unwrap();
        let mut returned = 0;
        let success = unsafe {
            DeviceIoControl(
                file.as_raw_handle(),
                0x000900a4,
                data.as_ptr().cast(),
                data.len() as u32,
                std::ptr::null_mut(),
                0,
                &mut returned,
                std::ptr::null_mut(),
            )
        };
        assert_ne!(
            success,
            0,
            "synthetic reparse setup failed: {:?}",
            std::io::Error::last_os_error().raw_os_error()
        );
    }
    #[cfg(windows)]
    #[test]
    fn file_access_native_create_race_never_follows_changed_parent() {
        let fixture = Fixture::new();
        let nested = fixture.root.root.join("nested");
        let outside = fixture.base.join("outside");
        inject(Stage::BeforeNativeCreate, move || {
            set_junction_in_place(&nested, &outside)
        });
        let result = FileAccessPolicy.write(
            &fixture.root,
            &fixture.relative("nested/note.txt"),
            b"ours",
            true,
            None,
        );
        fs::remove_dir(fixture.root.root.join("nested")).unwrap();
        assert!(result.is_err());
        assert_eq!(
            fs::read_dir(fixture.base.join("outside")).unwrap().count(),
            0
        );
    }

    #[cfg(windows)]
    #[test]
    fn file_access_unsupported_unc_root_returns_precise_error_before_io() {
        let root = AuthorizedRoot::for_host_path(
            Path::new(r"\\synthetic-host\synthetic-share"),
            Path::new(""),
        );
        let error = FileAccessPolicy
            .open_read(
                &root,
                &FileAccessPolicy.validate_relative("note.txt").unwrap(),
            )
            .err()
            .unwrap();
        assert!(error.starts_with("file_access_root_unsupported:"));
    }

    #[test]
    fn file_access_reads_and_hashes_same_verified_file() {
        let fixture = Fixture::new();
        fs::write(fixture.root.root.join("nested/note.txt"), "hello").unwrap();
        let file = FileAccessPolicy
            .open_read(&fixture.root, &fixture.relative(r"nested\note.txt"))
            .unwrap();
        assert_eq!(file.read_bytes(100).unwrap(), b"hello");
        assert_eq!(
            file.hash(100).unwrap(),
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
        assert!(file.read_bytes(4).is_err());
    }
    #[test]
    fn file_access_mutations_use_pinned_parent_and_checked_expected_content() {
        let fixture = Fixture::new();
        let policy = FileAccessPolicy;
        let path = fixture.relative("nested/note.txt");
        policy
            .write(&fixture.root, &path, b"hello", true, None)
            .unwrap();
        let hash = policy
            .open_read(&fixture.root, &path)
            .unwrap()
            .hash(100)
            .unwrap();
        assert!(policy
            .write(&fixture.root, &path, b"bad", true, None)
            .is_err());
        assert!(policy
            .write(&fixture.root, &path, b"bad", false, Some("old"))
            .is_err());
        policy
            .write(&fixture.root, &path, b"updated", false, Some(&hash))
            .unwrap();
        let hash = policy
            .open_read(&fixture.root, &path)
            .unwrap()
            .hash(100)
            .unwrap();
        policy
            .create_directory(&fixture.root, &fixture.relative("newdir"))
            .unwrap();
        let target = fixture.relative("newdir/moved.txt");
        policy
            .move_file(&fixture.root, &path, &target, Some(&hash))
            .unwrap();
        assert_eq!(
            fs::read(fixture.root.root.join("newdir/moved.txt")).unwrap(),
            b"updated"
        );
        assert!(policy
            .delete(&fixture.root, &fixture.relative("newdir"), None)
            .is_err());
        policy.delete(&fixture.root, &target, Some(&hash)).unwrap();
        policy
            .delete(&fixture.root, &fixture.relative("newdir"), None)
            .unwrap();
        assert!(!fixture.root.root.join("newdir").exists());
    }
    #[test]
    fn file_access_rejects_protected_root_and_hardlink_alias() {
        let fixture = Fixture::new();
        fs::create_dir(fixture.root.root.join("state")).unwrap();
        fs::write(
            fixture.root.root.join("state/data.txt"),
            "synthetic private marker",
        )
        .unwrap();
        fs::hard_link(
            fixture.root.root.join("state/data.txt"),
            fixture.root.root.join("alias.txt"),
        )
        .unwrap();
        assert!(FileAccessPolicy
            .open_read(&fixture.root, &fixture.relative("state/data.txt"))
            .is_err());
        assert!(FileAccessPolicy
            .open_read(&fixture.root, &fixture.relative("alias.txt"))
            .is_err());
    }
    #[cfg(windows)]
    #[test]
    fn file_access_parent_replaced_after_validation_is_rejected() {
        let fixture = Fixture::new();
        fs::write(
            fixture.base.join("outside/note.txt"),
            "synthetic private marker",
        )
        .unwrap();
        let root = fixture.root.root.clone();
        let outside = fixture.base.join("outside");
        inject(Stage::AfterValidate, move || {
            fs::rename(root.join("nested"), root.join("previous")).unwrap();
            junction(&root.join("nested"), &outside);
        });
        let result =
            FileAccessPolicy.open_read(&fixture.root, &fixture.relative("nested/note.txt"));
        fs::remove_dir(fixture.root.root.join("nested")).unwrap();
        assert!(result.is_err());
    }
    #[cfg(windows)]
    #[test]
    fn file_access_opened_object_and_parent_cannot_be_replaced_or_changed() {
        let fixture = Fixture::new();
        fs::write(fixture.root.root.join("nested/note.txt"), "hello").unwrap();
        let root = fixture.root.root.clone();
        inject(Stage::AfterOpen, move || {
            assert!(fs::rename(root.join("nested"), root.join("moved")).is_err());
            assert!(fs::rename(root.join("nested/note.txt"), root.join("old.txt")).is_err());
            assert!(fs::write(root.join("nested/note.txt"), "synthetic private marker").is_err());
        });
        let file = FileAccessPolicy
            .open_read(&fixture.root, &fixture.relative("nested/note.txt"))
            .unwrap();
        assert_eq!(file.read_bytes(100).unwrap(), b"hello");
    }
    #[test]
    fn file_access_create_race_preserves_external_file_and_parent() {
        let fixture = Fixture::new();
        let root = fixture.root.root.clone();
        inject(Stage::BeforeCreate, move || {
            assert!(fs::rename(root.join("nested"), root.join("moved")).is_err());
            fs::write(root.join("nested/note.txt"), "external synthetic file").unwrap();
        });
        assert!(FileAccessPolicy
            .write(
                &fixture.root,
                &fixture.relative("nested/note.txt"),
                b"ours",
                true,
                None
            )
            .is_err());
        assert_eq!(
            fs::read(fixture.root.root.join("nested/note.txt")).unwrap(),
            b"external synthetic file"
        );
        assert_eq!(
            fs::read_dir(fixture.root.root.join("nested"))
                .unwrap()
                .count(),
            1
        );
    }
}
