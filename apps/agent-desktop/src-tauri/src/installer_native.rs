//! Full-package, per-file differential installer. Never hardlinks runtime files.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const APP: &str = "io.github.zcchou.agent.airworthiness";
const OWNER: &str = "io.github.zcchou.agent.airworthiness.offline-runtime.v1";
const UNINSTALL_STATE: &str = ".airworthiness-uninstall.json";
const APP_FILES: &[&str] = &[
    "agent-desktop.exe",
    "agent-core.exe",
    "agent_desktop_lib.dll",
    "agent-updater.exe",
    "application.json",
    "uninstall.exe",
    "LICENSE.txt",
    "使用说明.md",
];

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Identity {
    bytes: u64,
    sha256: String,
    #[serde(default)]
    volume: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Archive {
    name: String,
    bytes: u64,
    sha256: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Manifest {
    schema_version: u32,
    #[serde(default)]
    application_id: String,
    release_id: String,
    files: BTreeMap<String, Identity>,
    #[serde(default)]
    archives: Vec<Archive>,
}
#[derive(Serialize, Deserialize)]
struct Journal {
    application_id: String,
    id: String,
    pid: u32,
    started: u64,
    previous: Option<Manifest>,
    next: Manifest,
}
#[derive(Serialize, Deserialize)]
struct Intent {
    name: String,
    old: Option<Identity>,
}
#[derive(Serialize, Deserialize)]
struct CleanupInventory {
    application_id: String,
    id: String,
    files: BTreeMap<String, Identity>,
    directories: BTreeSet<String>,
}
#[derive(Serialize, Deserialize)]
struct UninstallState {
    application_id: String,
    id: String,
    root: String,
    inventory: Manifest,
}
fn uninstall_state(root: &Path) -> Result<Option<UninstallState>> {
    let path = safe(root, UNINSTALL_STATE)?;
    if !path.exists() {
        return Ok(None);
    }
    let state: UninstallState = read_json(&path)?;
    if state.application_id != APP
        || state.id.len() != 32
        || !state.id.bytes().all(|c| c.is_ascii_hexdigit())
        || !root.to_string_lossy().eq_ignore_ascii_case(&state.root)
    {
        return Err("uninstall recovery ownership differs from installation".into());
    }
    validate(&state.inventory)?;
    Ok(Some(state))
}

#[derive(Clone, Default, Serialize)]
struct Status {
    phase: String,
    files_done: u64,
    files_total: u64,
    bytes_done: u64,
    bytes_total: u64,
    elapsed_ms: u128,
    percent: u8,
}
struct Progress {
    state: Arc<Mutex<Status>>,
    stopped: Arc<AtomicBool>,
    start: Instant,
}
impl Progress {
    fn new() -> Self {
        let state = Arc::new(Mutex::new(Status::default()));
        let stopped = Arc::new(AtomicBool::new(false));
        let (s, stop) = (state.clone(), stopped.clone());
        let start = Instant::now();
        std::thread::spawn(move || {
            while !stop.load(Ordering::Relaxed) {
                if let Ok(mut value) = s.lock() {
                    value.elapsed_ms = start.elapsed().as_millis();
                    println!("{}", serde_json::to_string(&*value).unwrap());
                }
                std::thread::sleep(Duration::from_millis(500));
            }
        });
        Self {
            state,
            stopped,
            start,
        }
    }
    fn phase(&self, phase: &str, total: usize, bytes: u64, percent: u8) {
        let mut s = self.state.lock().unwrap();
        *s = Status {
            phase: phase.into(),
            files_total: total as u64,
            bytes_total: bytes,
            percent,
            elapsed_ms: self.start.elapsed().as_millis(),
            ..Status::default()
        };
        if phase == "completed" {
            s.files_done = s.files_total;
            s.bytes_done = s.bytes_total;
        }
        println!("{}", serde_json::to_string(&*s).unwrap());
    }
    fn bytes(&self, count: usize) {
        self.state.lock().unwrap().bytes_done += count as u64;
    }
    fn file(&self) {
        self.state.lock().unwrap().files_done += 1;
    }
}
impl Drop for Progress {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::Relaxed);
    }
}

fn valid_name(name: &str) -> Result<()> {
    if name.is_empty() || name.contains('\\') || name.contains(':') {
        return Err("unsafe relative path".into());
    }
    for part in name.split('/') {
        let stem = part.split('.').next().unwrap_or("").to_ascii_uppercase();
        if part.is_empty()
            || part == "."
            || part == ".."
            || part.ends_with(['.', ' '])
            || part.chars().any(|c| c < ' ' || "<>\"|?*".contains(c))
            || ["CON", "PRN", "AUX", "NUL"].contains(&stem.as_str())
            || (stem.len() == 4
                && (stem.starts_with("COM") || stem.starts_with("LPT"))
                && matches!(stem.as_bytes()[3], b'1'..=b'9'))
        {
            return Err(format!("unsafe Windows path: {name}").into());
        }
    }
    Ok(())
}
fn plain(path: &Path) -> Result<()> {
    for ancestor in path.ancestors() {
        match fs::symlink_metadata(ancestor) {
            Ok(m) => {
                #[cfg(windows)]
                {
                    use std::os::windows::fs::MetadataExt;
                    if m.file_attributes() & 0x400 != 0 {
                        return Err("reparse path refused".into());
                    }
                }
                if m.file_type().is_symlink() {
                    return Err("linked path refused".into());
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(e) => return Err(e.into()),
        }
    }
    Ok(())
}
fn safe(root: &Path, name: &str) -> Result<PathBuf> {
    valid_name(name)?;
    let p = root.join(name);
    plain(&p)?;
    Ok(p)
}
fn allowed(name: &str) -> bool {
    APP_FILES.contains(&name)
        || matches!(
            name.split('/').next(),
            Some(
                "llm-runtime"
                    | "rag-runtime"
                    | "ocr-runtime"
                    | "ocr-model-package"
                    | "office-runtime"
                    | "prerequisites"
            )
        ) && name.contains('/')
}
fn validate(m: &Manifest) -> Result<()> {
    if m.schema_version != 2
        || m.application_id != APP
        || m.files.len() > 100_000
        || m.archives.len() > 64
    {
        return Err("wrong application inventory identity".into());
    }
    let mut seen = BTreeSet::new();
    for (name, record) in &m.files {
        valid_name(name)?;
        if !allowed(name)
            || !seen.insert(name.to_lowercase())
            || record.sha256.len() != 64
            || !record
                .sha256
                .bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        {
            return Err(format!("invalid inventory entry: {name}").into());
        }
    }
    let mut volumes = BTreeSet::new();
    for a in &m.archives {
        valid_name(&a.name)?;
        if a.name.contains('/')
            || !volumes.insert(a.name.to_lowercase())
            || a.bytes >= 2 * 1024 * 1024 * 1024
            || a.sha256.len() != 64
        {
            return Err("invalid volume identity".into());
        }
    }
    Ok(())
}
fn read_json<T: serde::de::DeserializeOwned>(p: &Path) -> Result<T> {
    plain(p)?;
    let data = fs::read(p)?;
    Ok(serde_json::from_slice(
        data.strip_prefix(&[239, 187, 191]).unwrap_or(&data),
    )?)
}
fn open_regular(path: &Path) -> Result<File> {
    plain(path)?;
    let file = File::open(path)?;
    if !file.metadata()?.is_file() {
        return Err("expected regular file".into());
    }
    #[cfg(windows)]
    {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Storage::FileSystem::{
            GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
        };
        let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
        if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut info) } == 0
            || info.nNumberOfLinks != 1
        {
            return Err("multiply linked runtime file refused".into());
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if file.metadata()?.nlink() != 1 {
            return Err("multiply linked file refused".into());
        }
    }
    Ok(file)
}
fn hash(path: &Path, progress: Option<&Progress>, allow_archive_links: bool) -> Result<Identity> {
    plain(path)?;
    let mut f = if allow_archive_links {
        File::open(path)?
    } else {
        open_regular(path)?
    };
    let mut h = Sha256::new();
    let mut total = 0;
    let mut buffer = vec![0; 1024 * 1024];
    loop {
        let count = f.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        total += count as u64;
        h.update(&buffer[..count]);
        if let Some(p) = progress {
            p.bytes(count);
        }
    }
    Ok(Identity {
        bytes: total,
        sha256: format!("{:x}", h.finalize()),
        volume: String::new(),
    })
}
fn equal(a: &Identity, b: &Identity) -> bool {
    a.bytes == b.bytes && a.sha256 == b.sha256
}
fn rename(from: &Path, to: &Path, replace: bool) -> Result<()> {
    plain(from)?;
    plain(to)?;
    if let Some(parent) = to.parent() {
        fs::create_dir_all(parent)?;
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::{
            MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
        };
        let from: Vec<u16> = from.as_os_str().encode_wide().chain(Some(0)).collect();
        let to: Vec<u16> = to.as_os_str().encode_wide().chain(Some(0)).collect();
        if unsafe {
            MoveFileExW(
                from.as_ptr(),
                to.as_ptr(),
                MOVEFILE_WRITE_THROUGH
                    | if replace {
                        MOVEFILE_REPLACE_EXISTING
                    } else {
                        0
                    },
            )
        } == 0
        {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    #[cfg(not(windows))]
    {
        if !replace && to.exists() {
            return Err("destination exists".into());
        }
        fs::rename(from, to)?;
        if let Some(p) = to.parent() {
            File::open(p)?.sync_all()?;
        }
    }
    Ok(())
}
fn write_json<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    plain(path)?;
    let temporary = path.with_extension("new");
    plain(&temporary)?;
    let mut file = OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .open(&temporary)?;
    file.write_all(&serde_json::to_vec(value)?)?;
    file.sync_all()?;
    drop(file);
    rename(&temporary, path, true)
}
fn write_new(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut f = OpenOptions::new().create_new(true).write(true).open(path)?;
    f.write_all(bytes)?;
    f.sync_all()?;
    Ok(())
}
fn process_started(pid: u32) -> Option<u64> {
    #[cfg(windows)]
    {
        use windows_sys::Win32::{
            Foundation::{CloseHandle, FILETIME},
            System::Threading::*,
        };
        unsafe {
            let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | 0x00100000, 0, pid);
            if process.is_null() {
                return None;
            }
            let mut a: FILETIME = std::mem::zeroed();
            let mut b = a;
            let mut c = a;
            let mut d = a;
            let alive = WaitForSingleObject(process, 0) == 258;
            let ok = GetProcessTimes(process, &mut a, &mut b, &mut c, &mut d) != 0;
            CloseHandle(process);
            return if ok && alive {
                Some(((a.dwHighDateTime as u64) << 32) | a.dwLowDateTime as u64)
            } else {
                None
            };
        }
    }
    #[cfg(not(windows))]
    {
        if pid == std::process::id() {
            Some(pid as u64)
        } else {
            None
        }
    }
}
fn canonical_path(path: &Path) -> Result<PathBuf> {
    let canonical = fs::canonicalize(path)?;
    #[cfg(windows)]
    {
        // Normalize long, short and extended Windows names to one comparison form.
        let value = canonical.to_string_lossy();
        if let Some(value) = value.strip_prefix("\\\\?\\UNC\\") {
            return Ok(PathBuf::from(format!("\\\\{value}")));
        }
        if let Some(value) = value.strip_prefix("\\\\?\\") {
            return Ok(PathBuf::from(value));
        }
    }
    Ok(canonical)
}
fn normalize_root(root: &Path) -> Result<PathBuf> {
    if !root.is_absolute()
        || root
            .to_string_lossy()
            .replace('\\', "/")
            .split('/')
            .any(|p| p == "." || p == "..")
    {
        return Err("ambiguous installation root refused".into());
    }
    plain(root)?;
    let mut existing = root;
    let mut pending = Vec::new();
    while !existing.exists() {
        pending.push(
            existing
                .file_name()
                .ok_or("invalid installation root")?
                .to_owned(),
        );
        existing = existing.parent().ok_or("invalid installation root")?;
    }
    let mut canonical = canonical_path(existing)?;
    for part in pending.iter().rev() {
        canonical.push(part);
    }
    Ok(canonical)
}
fn assert_idle(root: &Path) -> Result<()> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let output = std::process::Command::new("powershell.exe").args(["-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=New-Object Text.UTF8Encoding($false); ConvertTo-Json -Compress -InputObject @(Get-Process | Where-Object { $_.Path } | ForEach-Object { @{pid=$_.Id;path=$_.Path} })"])
            .creation_flags(0x08000000).output()?;
        if !output.status.success() {
            return Err(String::from_utf8_lossy(&output.stderr).into_owned().into());
        }
        #[derive(Deserialize)]
        struct Process {
            pid: u32,
            path: String,
        }
        let processes: Vec<Process> = serde_json::from_slice(&output.stdout)?;
        let prefix = format!(
            "{}\\",
            root.to_string_lossy().trim_end_matches('\\').to_lowercase()
        );
        for process in processes {
            let path = canonical_path(Path::new(&process.path))
                .unwrap_or_else(|_| PathBuf::from(&process.path));
            if path.to_string_lossy().to_lowercase().starts_with(&prefix) {
                return Err(format!(
                    "Close application and managed workers; busy PID {}",
                    process.pid
                )
                .into());
            }
        }
    }
    Ok(())
}
fn lock(managed: &Path) -> Result<File> {
    // Share the legacy installer's exclusive lock as well as the NSIS lifecycle mutex.
    let path = safe(managed, "install.lock")?;
    let mut options = OpenOptions::new();
    options.create(true).read(true).write(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.share_mode(0);
    }
    Ok(options
        .open(path)
        .map_err(|e| format!("another installer owns the installation lock: {e}"))?)
}
fn previous(root: &Path, managed: &Path) -> Result<Option<Manifest>> {
    let active = safe(managed, "native-active.json")?;
    if active.exists() {
        let m: Manifest = read_json(&active)?;
        validate(&m)?;
        return Ok(Some(m));
    }
    let legacy = safe(managed, "active.json")?;
    if !legacy.exists() {
        return Ok(None);
    }
    let mut m: Manifest = read_json(&legacy)?;
    if m.schema_version != 1 || !m.release_id.starts_with("agent-0.16.0-preview.") {
        return Err("unsupported legacy inventory".into());
    }
    m.schema_version = 2;
    m.application_id = APP.into();
    m.archives.clear();
    m.files
        .retain(|name, _| !name.starts_with("prerequisites/"));
    for name in APP_FILES {
        let p = safe(root, name)?;
        if p.exists() {
            m.files.insert((*name).into(), hash(&p, None, false)?);
        }
    }
    validate(&m)?;
    Ok(Some(m))
}
fn remove_empty_parents(path: &Path, stop: &Path) {
    let mut parent = path.parent();
    while let Some(p) = parent {
        if p == stop || !p.starts_with(stop) {
            break;
        }
        if fs::remove_dir(p).is_err() {
            break;
        }
        parent = p.parent();
    }
}
fn empty_cleanup_inventory() -> CleanupInventory {
    CleanupInventory {
        application_id: APP.into(),
        id: uuid::Uuid::new_v4().simple().to_string(),
        files: BTreeMap::new(),
        directories: BTreeSet::new(),
    }
}
fn cleanup_entry(plan: &mut CleanupInventory, name: String, identity: Identity) -> Result<()> {
    valid_name(&name)?;
    let mut parent = Path::new(&name).parent();
    while let Some(path) = parent {
        if path.as_os_str().is_empty() {
            break;
        }
        plan.directories
            .insert(path.to_string_lossy().replace('\\', "/"));
        parent = path.parent();
    }
    plan.files.insert(name, identity);
    Ok(())
}
fn control_entries(root: &Path, controls: &[&str], plan: &mut CleanupInventory) -> Result<()> {
    for name in controls {
        let path = safe(root, name)?;
        if path.is_file() {
            cleanup_entry(plan, (*name).into(), hash(&path, None, false)?)?;
        }
    }
    Ok(())
}
fn native_cleanup_inventory(root: &Path) -> Result<CleanupInventory> {
    let mut plan = empty_cleanup_inventory();
    let journal_path = safe(root, "journal.json")?;
    if journal_path.exists() {
        let journal: Journal = read_json(&journal_path)?;
        if journal.application_id != APP
            || journal.id.len() != 32
            || !journal.id.bytes().all(|c| c.is_ascii_hexdigit())
        {
            return Err("invalid cleanup transaction identity".into());
        }
        validate(&journal.next)?;
        if let Some(old) = &journal.previous {
            validate(old)?;
        }
        plan.id = journal.id;
        plan.directories.extend(["stage".into(), "backup".into()]);
        for (name, identity) in &journal.next.files {
            cleanup_entry(&mut plan, format!("stage/{name}"), identity.clone())?;
        }
        let operations = safe(root, "operations.jsonl")?;
        if operations.exists() {
            for line in fs::read_to_string(&operations)?.split_inclusive('\n') {
                if !line.ends_with('\n') {
                    continue;
                }
                let op: Intent = serde_json::from_str(line)?;
                if !journal.next.files.contains_key(&op.name)
                    && !journal
                        .previous
                        .as_ref()
                        .is_some_and(|m| m.files.contains_key(&op.name))
                {
                    return Err("cleanup operation is not owned".into());
                }
                if let Some(old) = op.old {
                    cleanup_entry(&mut plan, format!("backup/{}", op.name), old)?;
                }
            }
        }
        let health = safe(root, "health-inventory.json")?;
        if health.exists() {
            let health: CleanupInventory = read_json(&health)?;
            if health.application_id != APP || health.id != plan.id {
                return Err("health cleanup ownership mismatch".into());
            }
            plan.directories.insert("health-data".into());
            for directory in health.directories {
                valid_name(&directory)?;
                plan.directories.insert(format!("health-data/{directory}"));
            }
            for (name, record) in health.files {
                cleanup_entry(&mut plan, format!("health-data/{name}"), record)?;
            }
        }
    }
    control_entries(
        root,
        &[
            "owner.txt",
            "journal.json",
            "journal.new",
            "operations.jsonl",
            "committed",
            "postflight-owner.json",
            "postflight-owner.new",
            "health-inventory.json",
            "health-inventory.new",
        ],
        &mut plan,
    )?;
    Ok(plan)
}
fn clean_recorded(root: &Path, managed: &Path, inventory: &CleanupInventory) -> Result<()> {
    if inventory.application_id != APP
        || inventory.id.len() != 32
        || !inventory.id.bytes().all(|c| c.is_ascii_hexdigit())
    {
        return Err("wrong cleanup ownership".into());
    }
    plain(root)?;
    if !root.exists() {
        return Ok(());
    }
    for (name, expected) in &inventory.files {
        let path = safe(root, name)?;
        if path.is_file() && equal(&hash(&path, None, false)?, expected) {
            fs::remove_file(path)?;
        }
    }
    let mut directories: Vec<_> = inventory.directories.iter().collect();
    directories.sort_by_key(|name| std::cmp::Reverse(name.len()));
    for name in directories {
        let path = safe(root, name)?;
        if path.is_dir() && fs::read_dir(&path)?.next().is_none() {
            fs::remove_dir(path)?;
        }
    }
    if fs::read_dir(root)?.next().is_none() {
        fs::remove_dir(root)?;
    } else {
        // Unknown or changed files were never granted deletion ownership. Move the
        // remainder intact, making retry possible without silently discarding data.
        let preserved = safe(
            managed,
            &format!("preserved-native-{}", uuid::Uuid::new_v4().simple()),
        )?;
        rename(root, &preserved, false)?;
        println!("PRESERVED_UNKNOWN_NATIVE_DATA {}", preserved.display());
    }
    Ok(())
}
fn retire_recorded(
    root: &Path,
    managed: &Path,
    inventory: &CleanupInventory,
    requested_fault: &str,
) -> Result<()> {
    write_json(&managed.join("native-cleanup.json"), inventory)?;
    let garbage = managed.join("native-cleanup");
    rename(root, &garbage, false)?;
    fault(requested_fault, "after-cleanup-rename");
    clean_recorded(&garbage, managed, inventory)?;
    fs::remove_file(managed.join("native-cleanup.json"))?;
    Ok(())
}
fn record_health(root: &Path, transaction: &Path, id: &str) -> Result<()> {
    if !root.exists() {
        return Ok(());
    }
    let mut inventory = empty_cleanup_inventory();
    inventory.id = id.into();
    let mut pending = vec![root.to_path_buf()];
    while let Some(directory) = pending.pop() {
        for entry in fs::read_dir(directory)? {
            let path = entry?.path();
            plain(&path)?;
            let name = path
                .strip_prefix(root)?
                .to_string_lossy()
                .replace('\\', "/");
            if path.is_dir() {
                inventory.directories.insert(name);
                pending.push(path);
            } else {
                cleanup_entry(&mut inventory, name, hash(&path, None, false)?)?;
            }
        }
    }
    write_json(&transaction.join("health-inventory.json"), &inventory)
}
fn cleanup_legacy(managed: &Path, progress: &Progress) -> Result<()> {
    let versions = safe(managed, "versions")?;
    if !versions.exists() {
        return Ok(());
    }
    progress.phase("cleanup/legacy-backups", 0, 0, 99);
    for entry in fs::read_dir(&versions)? {
        let version = entry?.path();
        plain(&version)?;
        let name = version.file_name().and_then(|s| s.to_str()).unwrap_or("");
        if name.len() != 32 || !name.bytes().all(|c| c.is_ascii_hexdigit()) || !version.is_dir() {
            continue;
        }
        let marker = safe(&version, "owner.txt")?;
        if !marker.exists() || fs::read_to_string(&marker)?.trim() != OWNER {
            continue;
        }
        for (subtree, inventory) in [
            ("candidate", "payload-manifest.json"),
            ("failed", "payload-manifest.json"),
            ("previous", "previous-active.json"),
        ] {
            let record = safe(&version, inventory)?;
            let tree = safe(&version, subtree)?;
            if record.exists() && tree.exists() {
                let mut m: Manifest = read_json(&record)?;
                if m.schema_version != 1 {
                    return Err("invalid legacy backup inventory".into());
                }
                m.schema_version = 2;
                m.application_id = APP.into();
                validate(&m)?;
                for (name, expected) in &m.files {
                    if name.starts_with("prerequisites/") {
                        continue;
                    }
                    let path = safe(&tree, name)?;
                    if path.exists() && equal(&hash(&path, Some(progress), false)?, expected) {
                        fs::remove_file(&path)?;
                        remove_empty_parents(&path, &version);
                        progress.file();
                    }
                }
            }
            if tree.exists() {
                let _ = fs::remove_dir(&tree);
            }
        }
        let app_record = safe(&version, "previous-app.json")?;
        let app_tree = safe(&version, "previous-app")?;
        if app_record.exists() && app_tree.exists() {
            let files: BTreeMap<String, String> = read_json(&app_record)?;
            for (name, expected) in files {
                if !APP_FILES.contains(&name.as_str()) && name != "agent.exe" {
                    return Err("invalid legacy app backup".into());
                }
                let path = safe(&app_tree, &name)?;
                if path.exists() && hash(&path, Some(progress), false)?.sha256 == expected {
                    fs::remove_file(&path)?;
                    progress.file();
                }
            }
        }
        for name in [
            "candidate",
            "previous",
            "failed",
            "previous-app",
            "failed-app",
        ] {
            let path = safe(&version, name)?;
            if path.exists() {
                let _ = fs::remove_dir(path);
            }
        }
        let controls = [
            "owner.txt",
            "payload-manifest.json",
            "application-manifest.json",
            "previous-active.json",
            "previous-app.json",
        ];
        let remaining: Vec<_> =
            fs::read_dir(&version)?.collect::<std::result::Result<Vec<_>, _>>()?;
        if remaining.iter().all(|p| {
            p.file_name()
                .to_str()
                .is_some_and(|n| controls.contains(&n))
        }) {
            // No resource or unknown entry remains. Retire the metadata through the
            // same crash-resumable cleanup directory used for native transactions.
            let mut plan = empty_cleanup_inventory();
            control_entries(&version, &controls, &mut plan)?;
            retire_recorded(&version, managed, &plan, "")?;
        } else {
            println!("PRESERVED_UNKNOWN_LEGACY_DATA {}", version.display());
        }
    }
    let _ = fs::remove_dir(&versions);
    Ok(())
}
fn recover(root: &Path, managed: &Path, progress: &Progress) -> Result<()> {
    let garbage = safe(managed, "native-cleanup")?;
    let cleanup_record = safe(managed, "native-cleanup.json")?;
    if cleanup_record.exists() {
        let inventory: CleanupInventory = read_json(&cleanup_record)?;
        progress.phase("cleanup", 0, 0, 99);
        clean_recorded(&garbage, managed, &inventory)?;
        fs::remove_file(&cleanup_record)?;
    } else if garbage.exists() {
        return Err("unowned cleanup directory".into());
    }
    let preparation = safe(managed, "native-preparation")?;
    let preparation_record = safe(managed, "native-preparation.json")?;
    if preparation_record.exists() {
        let application: String = read_json(&preparation_record)?;
        if application != APP {
            return Err("wrong preparation ownership".into());
        }
        if preparation.exists() {
            retire_recorded(
                &preparation,
                managed,
                &native_cleanup_inventory(&preparation)?,
                "",
            )?;
        }
        fs::remove_file(&preparation_record)?;
    } else if preparation.exists() {
        return Err("unowned preparation directory".into());
    }
    let txn = safe(managed, "native-transaction")?;
    if !txn.exists() {
        return Ok(());
    }
    if fs::read_to_string(safe(&txn, "owner.txt")?)?.trim() != OWNER {
        return Err("wrong transaction ownership".into());
    }
    let journal: Journal = read_json(&safe(&txn, "journal.json")?)?;
    if journal.application_id != APP
        || journal.id.len() != 32
        || !journal.id.bytes().all(|c| c.is_ascii_hexdigit())
    {
        return Err("invalid transaction identity".into());
    }
    if journal.pid != std::process::id() && process_started(journal.pid) == Some(journal.started) {
        return Err("another live installer owns transaction".into());
    }
    #[cfg(windows)]
    {
        let child = safe(&txn, "postflight-owner.json")?;
        if child.exists() {
            crate::installer_process::recover(&read_json(&child)?, &journal.id)?;
        }
    }
    validate(&journal.next)?;
    if let Some(m) = &journal.previous {
        validate(m)?;
    }
    progress.phase("recovery", 0, 0, 1);
    if !safe(&txn, "committed")?.exists() {
        let operations = safe(&txn, "operations.jsonl")?;
        if operations.exists() {
            let raw = fs::read_to_string(&operations)?;
            let mut operations = Vec::new();
            // An incomplete final journal line cannot precede a rename: it was not durably flushed.
            for line in raw.split_inclusive('\n') {
                if line.ends_with('\n') {
                    operations.push(serde_json::from_str::<Intent>(line)?);
                }
            }
            for op in operations.iter().rev() {
                if !journal.next.files.contains_key(&op.name)
                    && !journal
                        .previous
                        .as_ref()
                        .is_some_and(|m| m.files.contains_key(&op.name))
                {
                    return Err("journal path is not owned".into());
                }
                let active = safe(root, &op.name)?;
                let backup = safe(&txn.join("backup"), &op.name)?;
                let stage = safe(&txn.join("stage"), &op.name)?;
                if backup.exists() {
                    let expected = op.old.as_ref().ok_or("unexpected backup")?;
                    if !equal(&hash(&backup, None, false)?, expected) {
                        return Err("rollback backup integrity mismatch".into());
                    }
                    if active.exists() {
                        if !journal
                            .next
                            .files
                            .get(&op.name)
                            .is_some_and(|r| hash(&active, None, false).is_ok_and(|a| equal(&a, r)))
                        {
                            return Err("active file changed during recovery; preserved".into());
                        }
                        fs::remove_file(&active)?;
                    }
                    rename(&backup, &active, false)?;
                } else if op.old.is_none() && !stage.exists() && active.exists() {
                    if !journal
                        .next
                        .files
                        .get(&op.name)
                        .is_some_and(|r| hash(&active, None, false).is_ok_and(|a| equal(&a, r)))
                    {
                        return Err("unowned recovery collision preserved".into());
                    }
                    fs::remove_file(&active)?;
                    remove_empty_parents(&active, root);
                }
            }
        }
        let active = safe(managed, "native-active.json")?;
        if let Some(m) = journal.previous {
            write_json(&active, &m)?;
        } else if active.exists() {
            fs::remove_file(active)?;
        }
    }
    finish_cleanup(managed, progress, "")
}
fn finish_cleanup(managed: &Path, progress: &Progress, requested_fault: &str) -> Result<()> {
    progress.phase("cleanup", 0, 0, 99);
    fault(requested_fault, "before-cleanup");
    let transaction = managed.join("native-transaction");
    retire_recorded(
        &transaction,
        managed,
        &native_cleanup_inventory(&transaction)?,
        requested_fault,
    )
}
fn check_space(root: &Path, bytes: u64) -> Result<()> {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        use windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW;
        let path: Vec<u16> = root.as_os_str().encode_wide().chain(Some(0)).collect();
        let mut available = 0;
        if unsafe {
            GetDiskFreeSpaceExW(
                path.as_ptr(),
                &mut available,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        } == 0
        {
            return Err(std::io::Error::last_os_error().into());
        }
        if available < bytes.saturating_add(128 * 1024 * 1024) {
            return Err(format!(
                "insufficient space: need {bytes} changed bytes plus recovery reserve"
            )
            .into());
        }
    }
    Ok(())
}
fn resolve_volumes(
    manifest: &Manifest,
    names: &BTreeSet<String>,
    downloads: &Path,
    progress: &Progress,
) -> Result<BTreeMap<String, PathBuf>> {
    plain(downloads)?;
    let required: Vec<_> = manifest
        .archives
        .iter()
        .filter(|a| names.contains(&a.name))
        .collect();
    if required.len() != names.len() {
        return Err("file references missing volume identity".into());
    }
    progress.phase(
        "volume-hashes",
        required.len(),
        required.iter().map(|a| a.bytes).sum(),
        20,
    );
    let candidates: Vec<_> = fs::read_dir(downloads)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|e| e.eq_ignore_ascii_case("zip")))
        .collect();
    let mut cache = BTreeMap::<PathBuf, Identity>::new();
    let mut result = BTreeMap::new();
    let mut missing = Vec::new();
    for archive in required {
        let preferred = safe(downloads, &archive.name)?;
        for path in std::iter::once(&preferred).chain(candidates.iter()) {
            if !path.exists() || fs::metadata(path)?.len() != archive.bytes {
                continue;
            }
            let actual = if let Some(value) = cache.get(path) {
                value.clone()
            } else {
                let value = hash(path, Some(progress), true)?;
                cache.insert(path.clone(), value.clone());
                value
            };
            if actual.sha256 == archive.sha256 {
                result.insert(archive.name.clone(), path.clone());
                break;
            }
        }
        if !result.contains_key(&archive.name) {
            missing.push(format!(
                "{} ({} bytes, sha256 {})",
                archive.name, archive.bytes, archive.sha256
            ));
        }
        progress.file();
    }
    if !missing.is_empty() {
        return Err(format!(
            "Missing or damaged required volumes:\n{}",
            missing.join("\n")
        )
        .into());
    }
    Ok(result)
}
fn copy_verified(
    mut input: impl Read,
    destination: &Path,
    record: &Identity,
    progress: &Progress,
) -> Result<()> {
    plain(destination)?;
    fs::create_dir_all(destination.parent().ok_or("missing parent")?)?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(destination)?;
    let mut digest = Sha256::new();
    let mut size = 0;
    let mut buffer = vec![0; 1024 * 1024];
    loop {
        let count = input.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        size += count as u64;
        if size > record.bytes {
            return Err("archive entry expands beyond trusted size".into());
        }
        output.write_all(&buffer[..count])?;
        digest.update(&buffer[..count]);
        progress.bytes(count);
    }
    output.sync_all()?;
    if size != record.bytes || format!("{:x}", digest.finalize()) != record.sha256 {
        return Err("staged file identity mismatch".into());
    }
    progress.file();
    Ok(())
}
fn fault(requested: &str, point: &str) {
    #[cfg(debug_assertions)]
    if requested == point {
        std::process::exit(97);
    }
    #[cfg(not(debug_assertions))]
    let _ = (requested, point);
}
pub fn main_entry() -> Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.len() % 2 != 0 {
        return Err("expected --key value pairs".into());
    }
    let args: BTreeMap<_, _> = args
        .chunks(2)
        .map(|a| (a[0].clone(), a[1].clone()))
        .collect();
    let get = |key: &str| {
        args.get(key)
            .map(String::as_str)
            .ok_or_else(|| format!("missing {key}"))
    };
    let root = normalize_root(Path::new(get("--root")?))?;
    if !root.is_absolute() || root.parent().is_none() || root.components().count() < 3 {
        return Err("unsafe installation root".into());
    }
    plain(&root)?;
    assert_idle(&root)?;
    let progress = Progress::new();
    progress.phase("preflight/difference", 0, 0, 0);
    let action = get("--action")?;
    // This root-bound full inventory survives removal of managed metadata and the
    // native/NSIS handoff. An absent inventory alone never authorizes an uninstall.
    let uninstall = uninstall_state(&root)?;
    let managed = safe(&root, ".managed-runtime")?;
    if managed.exists() {
        let owner = safe(&managed, "owner.txt")?;
        if !owner.exists() && uninstall.is_some() {
            // Resume a crash between owner removal and removal of its empty directory.
            write_new(&owner, OWNER.as_bytes())?;
        }
        if fs::read_to_string(owner)?.trim() != OWNER {
            return Err("wrong managed ownership".into());
        }
    } else {
        if action == "uninstall" && uninstall.is_none() {
            return Err("no owned inventory to uninstall".into());
        }
        fs::create_dir_all(&root)?;
        // Publish ownership metadata atomically. A crash before this rename leaves
        // only an unreferenced tiny staging directory, never an unowned active root.
        let initial = safe(
            &root,
            &format!(".managed-runtime-init-{}", uuid::Uuid::new_v4().simple()),
        )?;
        fs::create_dir(&initial)?;
        write_new(&initial.join("owner.txt"), OWNER.as_bytes())?;
        rename(&initial, &managed, false)?;
    }
    let _guard = lock(&managed)?;
    recover(&root, &managed, &progress)?;
    if safe(&managed, "transaction.json")?.exists() {
        return Err(
            "legacy interrupted transaction: run the previous installer recovery before continuing"
                .into(),
        );
    }
    if action == "recover" {
        return Ok(());
    }
    let previous = previous(&root, &managed)?
        .or_else(|| uninstall.as_ref().map(|state| state.inventory.clone()));
    if action == "uninstall" {
        let old = previous.ok_or("no owned inventory to uninstall")?;
        write_json(
            &root.join(UNINSTALL_STATE),
            &UninstallState {
                application_id: APP.into(),
                id: uninstall
                    .as_ref()
                    .map(|s| s.id.clone())
                    .unwrap_or_else(|| uuid::Uuid::new_v4().simple().to_string()),
                root: root.to_string_lossy().into_owned(),
                inventory: old.clone(),
            },
        )?;
        let uninstall_count = old
            .files
            .keys()
            .filter(|name| name.as_str() != "uninstall.exe")
            .count();
        progress.phase("cleanup", uninstall_count, 0, 1);
        for name in old.files.keys() {
            // NSIS removes its installed retry entry point only after native cleanup
            // succeeds. A late metadata error or interruption must leave it runnable.
            if name == "uninstall.exe" {
                continue;
            }
            let path = safe(&root, name)?;
            // Ownership is an inventory property; a normal program update can legitimately
            // change executable hashes. Unknown paths are never enumerated or removed.
            if path.exists() {
                let _ = open_regular(&path)?;
                fs::remove_file(&path)?;
                remove_empty_parents(&path, &root);
            }
            progress.file();
        }
        cleanup_legacy(&managed, &progress)?;
        for name in [
            "native-active.json",
            "active.json",
            "active.json.previous",
            "active.json.new",
        ] {
            let p = safe(&managed, name)?;
            if p.exists() {
                fs::remove_file(p)?;
            }
        }
        fault(
            args.get("--fault").map(String::as_str).unwrap_or(""),
            "uninstall-after-inventory",
        );
        drop(_guard);
        fs::remove_file(managed.join("install.lock"))?;
        let prior_lock = safe(&managed, "native.lock")?;
        if prior_lock.exists() {
            fs::remove_file(prior_lock)?;
        }
        // Unknown or modified legacy backup data remains available for inspection.
        if fs::read_dir(&managed)?.count() == 1 {
            fs::remove_file(managed.join("owner.txt"))?;
            fault(
                args.get("--fault").map(String::as_str).unwrap_or(""),
                "uninstall-after-owner",
            );
            fs::remove_dir(&managed)?;
        }
        fault(
            args.get("--fault").map(String::as_str).unwrap_or(""),
            "uninstall-before-nsis-tail",
        );
        progress.phase("completed", uninstall_count, 0, 100);
        return Ok(());
    }
    if action != "install" {
        return Err("unknown action".into());
    }
    let manifest_path = PathBuf::from(get("--manifest")?);
    if hash(&manifest_path, None, false)?.sha256 != get("--sha256")? {
        return Err("manifest differs from trusted installer digest".into());
    }
    let mut manifest: Manifest = read_json(&manifest_path)?;
    validate(&manifest)?;
    // The uninstaller is generated by this same trusted NSIS image after its digest
    // was embedded, so its identity is handed off separately from the build manifest.
    if let Some(uninstaller) = args.get("--uninstaller") {
        let mut record = hash(Path::new(uninstaller), None, false)?;
        if Path::new(uninstaller) != Path::new(get("--embedded")?).join("uninstall.exe") {
            return Err("uninstaller handoff path mismatch".into());
        }
        record.volume = "embedded".into();
        manifest.files.insert("uninstall.exe".into(), record);
    }
    let empty = BTreeMap::new();
    let old_files = previous.as_ref().map(|m| &m.files).unwrap_or(&empty);
    let mut changed = BTreeSet::new();
    let mut reuse = BTreeSet::new();
    progress.phase(
        "preflight/difference",
        manifest.files.len(),
        manifest.files.values().map(|r| r.bytes).sum(),
        1,
    );
    for (name, record) in &manifest.files {
        let path = safe(&root, name)?;
        if path.exists() && !old_files.contains_key(name) {
            return Err(format!("unowned destination preserved: {name}").into());
        }
        if path.exists() && equal(&hash(&path, Some(&progress), false)?, record) {
            reuse.insert(name.clone());
        } else {
            changed.insert(name.clone());
        }
        progress.file();
    }
    let deleted: BTreeSet<_> = old_files
        .keys()
        .filter(|name| !manifest.files.contains_key(*name))
        .cloned()
        .collect();
    check_space(&root, changed.iter().map(|n| manifest.files[n].bytes).sum())?;
    let required: BTreeSet<_> = changed
        .iter()
        .map(|name| manifest.files[name].volume.clone())
        .filter(|v| v != "embedded")
        .collect();
    let volumes = resolve_volumes(
        &manifest,
        &required,
        Path::new(get("--archives")?),
        &progress,
    )?;
    let txn = safe(&managed, "native-transaction")?;
    let preparation = safe(&managed, "native-preparation")?;
    let preparation_record = safe(&managed, "native-preparation.json")?;
    write_json(&preparation_record, &APP)?;
    fs::create_dir(&preparation)?;
    write_new(&preparation.join("owner.txt"), OWNER.as_bytes())?;
    let journal = Journal {
        application_id: APP.into(),
        id: uuid::Uuid::new_v4().simple().to_string(),
        pid: std::process::id(),
        started: process_started(std::process::id()).ok_or("cannot identify installer process")?,
        previous,
        next: manifest.clone(),
    };
    let fault_name = args.get("--fault").map(String::as_str).unwrap_or("");
    fault(fault_name, "before-prepare-journal");
    write_json(&preparation.join("journal.json"), &journal)?;
    fault(fault_name, "after-prepare-journal");
    fault(fault_name, "before-prepare-rename");
    rename(&preparation, &txn, false)?;
    fault(fault_name, "after-prepare-rename");
    fs::remove_file(&preparation_record)?;
    let install_result = (|| -> Result<()> {
        let stage = txn.join("stage");
        fs::create_dir(&stage)?;
        progress.phase(
            "extraction",
            changed.len(),
            changed.iter().map(|n| manifest.files[n].bytes).sum(),
            40,
        );
        for (volume, path) in volumes {
            let mut archive = zip::ZipArchive::new(File::open(path)?)?;
            let mut seen = BTreeSet::new();
            for index in 0..archive.len() {
                let entry = archive.by_index(index)?;
                let name = entry.name().to_owned();
                valid_name(&name)?;
                if !seen.insert(name.to_lowercase()) {
                    return Err("duplicate archive entry".into());
                }
                let record = manifest
                    .files
                    .get(&name)
                    .ok_or("unexpected archive entry")?;
                if record.volume != volume
                    || record.bytes != entry.size()
                    || entry.is_dir()
                    || entry.unix_mode().is_some_and(|m| m & 0o170000 == 0o120000)
                {
                    return Err("invalid mapped archive entry".into());
                }
                if changed.contains(&name) {
                    copy_verified(entry, &safe(&stage, &name)?, record, &progress)?;
                }
            }
        }
        for name in &changed {
            let record = &manifest.files[name];
            if record.volume == "embedded" {
                copy_verified(
                    open_regular(&safe(Path::new(get("--embedded")?), name)?)?,
                    &safe(&stage, name)?,
                    record,
                    &progress,
                )?;
            }
            if !safe(&stage, name)?.exists() {
                return Err(format!("missing staged file: {name}").into());
            }
        }
        assert_idle(&root)?;
        progress.phase(
            "reuse-verification",
            reuse.len(),
            reuse.iter().map(|n| manifest.files[n].bytes).sum(),
            60,
        );
        for name in &reuse {
            if !equal(
                &hash(&safe(&root, name)?, Some(&progress), false)?,
                &manifest.files[name],
            ) {
                return Err("reused file changed during staging".into());
            }
            progress.file();
        }
        let mutations: Vec<_> = changed.union(&deleted).cloned().collect();
        progress.phase("publication", mutations.len(), 0, 75);
        let mut log = OpenOptions::new()
            .create_new(true)
            .append(true)
            .open(txn.join("operations.jsonl"))?;
        for (index, name) in mutations.iter().enumerate() {
            let active = safe(&root, name)?;
            let old = if active.exists() {
                Some(hash(&active, None, false)?)
            } else {
                None
            };
            let op = Intent {
                name: name.clone(),
                old,
            };
            serde_json::to_writer(&mut log, &op)?;
            log.write_all(b"\n")?;
            log.sync_all()?;
            fault(fault_name, &format!("before-backup:{index}"));
            if active.exists() {
                rename(&active, &safe(&txn.join("backup"), name)?, false)?;
            }
            fault(fault_name, &format!("after-backup:{index}"));
            fault(fault_name, &format!("before-publish:{index}"));
            if changed.contains(name) {
                rename(&safe(&stage, name)?, &active, false)?;
            }
            fault(fault_name, &format!("after-publish:{index}"));
            progress.file();
        }
        drop(log);
        progress.phase("prerequisites/health", 0, 0, 90);
        if let Some(script) = args.get("--postflight") {
            #[cfg(windows)]
            {
                let health = safe(&txn, "health-data")?;
                if health.exists() {
                    return Err("unowned health directory collision".into());
                }
                let executable = PathBuf::from(
                    std::env::var_os("SystemRoot").ok_or("missing Windows directory")?,
                )
                .join("System32/WindowsPowerShell/v1.0/powershell.exe");
                let arguments = vec![
                    "-NoProfile".into(),
                    "-NonInteractive".into(),
                    "-ExecutionPolicy".into(),
                    "Bypass".into(),
                    "-File".into(),
                    script.clone(),
                    "-InstallDirectory".into(),
                    root.to_string_lossy().into_owned(),
                    "-ManifestPath".into(),
                    manifest_path.to_string_lossy().into_owned(),
                    "-HealthDirectory".into(),
                    txn.join("health-data").to_string_lossy().into_owned(),
                ];
                let child =
                    crate::installer_process::Child::spawn(&executable, &arguments, &journal.id)?;
                write_json(&txn.join("postflight-owner.json"), child.identity())?;
                let code = child.resume_and_wait()?;
                if code != 0 {
                    return Err("prerequisites or isolated Core health failed".into());
                }
                record_health(&health, &txn, &journal.id)?;
            }
            #[cfg(not(windows))]
            let _ = script;
        }
        assert_idle(&root)?;
        fault(fault_name, "before-inventory");
        write_json(&managed.join("native-active.json"), &manifest)?;
        fault(fault_name, "after-inventory");
        fault(fault_name, "before-commit");
        write_new(&txn.join("committed"), journal.id.as_bytes())?;
        fault(fault_name, "after-commit");
        Ok(())
    })();
    if let Err(error) = install_result {
        recover(&root, &managed, &progress)?;
        return Err(error);
    }
    finish_cleanup(&managed, &progress, fault_name)?;
    cleanup_legacy(&managed, &progress)?;
    // A successful reinstall supersedes an interrupted uninstall's inventory.
    if uninstall.is_some() {
        fs::remove_file(root.join(UNINSTALL_STATE))?;
    }
    for name in &deleted {
        remove_empty_parents(&root.join(name), &root);
    }
    progress.phase(
        "completed",
        manifest.files.len(),
        manifest.files.values().map(|r| r.bytes).sum(),
        100,
    );
    Ok(())
}
