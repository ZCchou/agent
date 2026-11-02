//! Bounded, cooperative local-disk search. Capabilities and cancellation are Host-owned.
use crate::file_access::{AuthorizedRoot, FileAccessPolicy, VerifiedReadHandle};
use aes_gcm::{aead::Aead, Aes256Gcm, KeyInit, Nonce};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicU8, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};

const CACHE_BYTES: usize = 16 * 1024 * 1024;
const CACHE_COUNT: usize = 32;
const TTL: Duration = Duration::from_secs(60);
pub type SearchError = String;
fn error(code: &str) -> String {
    format!("{code}: File search could not complete")
}

#[derive(Clone, Debug, Deserialize)]
pub struct SearchRequest {
    pub path: String,
    pub query: String,
    pub mode: String,
    #[serde(default = "default_limit")]
    pub limit: usize,
    pub cursor: Option<String>,
}
fn default_limit() -> usize {
    200
}
impl SearchRequest {
    pub fn parse(value: &serde_json::Value) -> Result<Self, SearchError> {
        if value
            .get("cursor")
            .is_some_and(|v| !v.is_null() && (!v.is_string() || v.as_str().unwrap().len() > 256))
        {
            return Err(error("invalid_search_cursor"));
        }
        let mut request: Self =
            serde_json::from_value(value.clone()).map_err(|_| error("invalid_tool_call"))?;
        if !matches!(request.mode.as_str(), "name" | "content")
            || request.limit == 0
            || request.limit > 1000
            || request.query.is_empty()
            || request.query.len() > 4096
        {
            return Err(error("invalid_tool_call"));
        }
        let path = FileAccessPolicy.validate_relative(&request.path)?;
        request.path = path.as_path().to_string_lossy().replace('\\', "/");
        if request.path.is_empty() {
            request.path = ".".into();
        }
        Ok(request)
    }
}

#[derive(Clone, Copy)]
struct SearchBudget {
    files: usize,
    entries: usize,
    depth: usize,
    bytes: usize,
    file_bytes: usize,
    duration: Duration,
}
impl Default for SearchBudget {
    fn default() -> Self {
        Self {
            files: 10000,
            entries: 20000,
            depth: 128,
            bytes: 67108864,
            file_bytes: 1048576,
            duration: Duration::from_millis(2000),
        }
    }
}
#[derive(Clone, Debug, Serialize)]
pub struct SearchPage {
    pub matches: Vec<String>,
    pub inspected_files: usize,
    pub next_cursor: Option<String>,
    pub complete: bool,
    pub truncated: bool,
    pub stop_reason: Option<String>,
}

#[derive(Clone, Default)]
pub struct Cancellation(Arc<AtomicU8>);
impl Cancellation {
    fn cancel(&self, reason: u8) {
        let _ = self
            .0
            .compare_exchange(0, reason, Ordering::SeqCst, Ordering::SeqCst);
    }
    pub fn check(&self) -> Result<(), SearchError> {
        match self.0.load(Ordering::SeqCst) {
            0 => Ok(()),
            1 => Err(error("cancelled")),
            _ => Err(error("authorization_changed")),
        }
    }
}

#[derive(Clone, PartialEq, Eq)]
struct Binding {
    run: String,
    project: String,
    conversation: String,
    generation: u64,
    policy: u32,
    root: std::path::PathBuf,
    path: String,
    query: String,
    mode: String,
    limit: usize,
}
struct Snapshot {
    binding: Binding,
    page: SearchPage,
    created: Instant,
    used: u64,
    bytes: usize,
    cursors: Vec<String>,
}
struct Task {
    cancellation: Cancellation,
    run: String,
    project: Option<String>,
    conversation: Option<String>,
}
#[derive(Default)]
struct State {
    tasks: HashMap<String, Task>,
    snapshots: HashMap<String, Snapshot>,
    clock: u64,
    core_generation: u64,
    cancelled_runs: HashMap<String, Instant>,
    changing_conversations: HashMap<String, usize>,
}
#[derive(Clone)]
pub struct FileSearchService {
    state: Arc<Mutex<State>>,
    slots: Arc<tokio::sync::Semaphore>,
    admission: Arc<tokio::sync::Semaphore>,
    cipher: Arc<Aes256Gcm>,
}
impl Default for FileSearchService {
    fn default() -> Self {
        let mut key = [0u8; 32];
        key[..16].copy_from_slice(uuid::Uuid::new_v4().as_bytes());
        key[16..].copy_from_slice(uuid::Uuid::new_v4().as_bytes());
        Self {
            cipher: Arc::new(Aes256Gcm::new_from_slice(&key).expect("fixed key length")),
            state: Arc::default(),
            slots: Arc::new(tokio::sync::Semaphore::new(2)),
            admission: Arc::new(tokio::sync::Semaphore::new(10)),
        }
    }
}
pub struct SearchTask {
    service: FileSearchService,
    id: String,
    run: String,
    pub cancellation: Cancellation,
}
pub struct ScopeChange {
    service: FileSearchService,
    conversation: String,
}
impl Drop for ScopeChange {
    fn drop(&mut self) {
        let mut state = self.service.state.lock().unwrap();
        cancel_conversation(&mut state, &self.conversation);
        if let Some(count) = state.changing_conversations.get_mut(&self.conversation) {
            *count -= 1;
            if *count == 0 {
                state.changing_conversations.remove(&self.conversation);
            }
        }
    }
}
fn invalidate_all(state: &mut State) {
    for task in state.tasks.values() {
        task.cancellation.cancel(1);
    }
    state.snapshots.clear();
}
fn cancel_conversation(state: &mut State, conversation: &str) {
    for task in state
        .tasks
        .values()
        .filter(|t| t.conversation.is_none() || t.conversation.as_deref() == Some(conversation))
    {
        task.cancellation.cancel(2);
    }
    state
        .snapshots
        .retain(|_, s| s.binding.conversation != conversation);
}
impl Drop for SearchTask {
    fn drop(&mut self) {
        self.cancellation.cancel(1);
        self.service.state.lock().unwrap().tasks.remove(&self.id);
    }
}
pub(crate) enum CoreEventAction<'a> {
    Observe,
    Search(&'a str),
    CancelRun(&'a str),
}

impl FileSearchService {
    pub(crate) fn admit_core_event(
        &self,
        generation: u64,
        action: CoreEventAction<'_>,
    ) -> Result<Option<SearchTask>, ()> {
        let mut state = self.state.lock().unwrap();
        if state.core_generation != generation {
            return Err(());
        }
        match action {
            CoreEventAction::Observe => Ok(None),
            CoreEventAction::Search(run) => Ok(Some(self.register_locked(&mut state, run))),
            CoreEventAction::CancelRun(run) => {
                Self::cancel_run_locked(&mut state, run);
                Ok(None)
            }
        }
    }

    #[cfg(test)]
    fn register(&self, run: &str) -> SearchTask {
        self.register_locked(&mut self.state.lock().unwrap(), run)
    }
    // Called only while admission owns the generation/state mutex.
    fn register_locked(&self, state: &mut State, run: &str) -> SearchTask {
        let id = uuid::Uuid::new_v4().to_string();
        let cancellation = Cancellation::default();
        let now = Instant::now();
        state
            .cancelled_runs
            .retain(|_, at| now.duration_since(*at) < TTL);
        if state.cancelled_runs.contains_key(run) {
            cancellation.cancel(1);
        }
        state.tasks.insert(
            id.clone(),
            Task {
                cancellation: cancellation.clone(),
                run: run.into(),
                project: None,
                conversation: None,
            },
        );
        SearchTask {
            service: self.clone(),
            id,
            run: run.into(),
            cancellation,
        }
    }
    pub fn bind_project(
        &self,
        task: &SearchTask,
        project: &str,
        conversation: &str,
    ) -> Result<(), SearchError> {
        let mut state = self.state.lock().unwrap();
        task.cancellation.check()?;
        if state.changing_conversations.contains_key(conversation) {
            return Err(error("authorization_changed"));
        }
        let task = state
            .tasks
            .get_mut(&task.id)
            .ok_or_else(|| error("cancelled"))?;
        task.project = Some(project.into());
        task.conversation = Some(conversation.into());
        Ok(())
    }
    pub fn commit<T>(
        &self,
        task: &SearchTask,
        publish: impl FnOnce() -> Result<T, SearchError>,
    ) -> Result<T, SearchError> {
        let _state = self.state.lock().unwrap();
        task.cancellation.check()?;
        publish()
    }
    pub fn cancel_run(&self, run: &str) {
        Self::cancel_run_locked(&mut self.state.lock().unwrap(), run);
    }
    fn cancel_run_locked(state: &mut State, run: &str) {
        if state.cancelled_runs.len() >= 1024 {
            if let Some(oldest) = state
                .cancelled_runs
                .iter()
                .min_by_key(|(_, at)| **at)
                .map(|(run, _)| run.clone())
            {
                state.cancelled_runs.remove(&oldest);
            }
        }
        state.cancelled_runs.insert(run.into(), Instant::now());
        for task in state.tasks.values().filter(|t| t.run == run) {
            task.cancellation.cancel(1);
        }
        state.snapshots.retain(|_, s| s.binding.run != run);
    }
    pub fn invalidate_project(&self, project: &str) {
        let mut state = self.state.lock().unwrap();
        for task in state
            .tasks
            .values()
            .filter(|t| t.project.is_none() || t.project.as_deref() == Some(project))
        {
            task.cancellation.cancel(2);
        }
        state.snapshots.retain(|_, s| s.binding.project != project);
    }
    pub fn changing_conversation(&self, conversation: &str) -> ScopeChange {
        let mut state = self.state.lock().unwrap();
        *state
            .changing_conversations
            .entry(conversation.into())
            .or_default() += 1;
        cancel_conversation(&mut state, conversation);
        ScopeChange {
            service: self.clone(),
            conversation: conversation.into(),
        }
    }
    pub fn begin_core_generation(&self) -> u64 {
        let mut state = self.state.lock().unwrap();
        state.core_generation += 1;
        invalidate_all(&mut state);
        state.core_generation
    }
    pub fn end_core_generation(&self, generation: u64) {
        let mut state = self.state.lock().unwrap();
        if state.core_generation == generation {
            state.core_generation += 1;
            invalidate_all(&mut state);
        }
    }
    pub fn invalidate_all(&self) {
        let mut state = self.state.lock().unwrap();
        // Shutdown also closes admission for a reader paused before dispatch.
        state.core_generation += 1;
        invalidate_all(&mut state);
    }
    pub async fn search(
        &self,
        root: AuthorizedRoot,
        request: SearchRequest,
        task: &SearchTask,
    ) -> Result<SearchPage, SearchError> {
        task.cancellation.check()?;
        let binding = Binding {
            run: task.run.clone(),
            project: root.project_id.clone(),
            conversation: root.conversation_id.clone(),
            generation: root.root_generation,
            policy: root.policy_version,
            root: root.root.clone(),
            path: request.path.clone(),
            query: request.query.clone(),
            mode: request.mode.clone(),
            limit: request.limit,
        };
        let cursor = request.cursor.clone();
        self.search_with(binding, cursor, task, move |cancellation| {
            scan(
                &Disk(root),
                &request,
                &cancellation,
                SearchBudget::default(),
                &Instant::now(),
            )
        })
        .await
    }
    async fn search_with<F>(
        &self,
        binding: Binding,
        cursor: Option<String>,
        task: &SearchTask,
        scan: F,
    ) -> Result<SearchPage, SearchError>
    where
        F: FnOnce(Cancellation) -> Result<SearchPage, SearchError> + Send + 'static,
    {
        task.cancellation.check()?;
        if let Some(cursor) = cursor {
            return self.cached(&binding, &cursor, &task.cancellation, Instant::now());
        }
        let admission = self
            .admission
            .clone()
            .try_acquire_owned()
            .map_err(|_| error("search_busy"))?;
        // Queued work occupies no blocking thread. Cancellation is checked while waiting.
        let slot = loop {
            task.cancellation.check()?;
            match tokio::time::timeout(
                Duration::from_millis(25),
                self.slots.clone().acquire_owned(),
            )
            .await
            {
                Ok(result) => break result.map_err(|_| error("search_busy"))?,
                Err(_) => continue,
            }
        };
        task.cancellation.check()?;
        let cancellation = task.cancellation.clone();
        let page = tokio::task::spawn_blocking(move || {
            // Permits stay in the worker even if its async waiter is dropped. No abort illusion.
            let (_admission, _slot) = (admission, slot);
            scan(cancellation)
        })
        .await
        .map_err(|_| error("search_worker_failed"))??;
        self.cache(binding, page, &task.cancellation, Instant::now())
    }
    fn cache(
        &self,
        binding: Binding,
        page: SearchPage,
        cancellation: &Cancellation,
        now: Instant,
    ) -> Result<SearchPage, SearchError> {
        let id = uuid::Uuid::new_v4().simple().to_string();
        let snapshot = self.snapshot(binding, page, now, &id)?;
        let bytes = snapshot.bytes;
        let mut state = self.state.lock().unwrap();
        cancellation.check()?;
        state
            .snapshots
            .retain(|_, s| now.duration_since(s.created) < TTL);
        state.clock += 1;
        let used = state.clock;
        while state.snapshots.len() >= CACHE_COUNT
            || state.snapshots.values().map(|s| s.bytes).sum::<usize>() + bytes > CACHE_BYTES
        {
            let Some(oldest) = state
                .snapshots
                .iter()
                .min_by_key(|(_, s)| s.used)
                .map(|(id, _)| id.clone())
            else {
                return Err(error("search_result_limit"));
            };
            state.snapshots.remove(&oldest);
        }
        let result = page_at(&snapshot, 0);
        if result.next_cursor.is_some() {
            state.snapshots.insert(id, Snapshot { used, ..snapshot });
        }
        Ok(result)
    }
    fn snapshot(
        &self,
        binding: Binding,
        page: SearchPage,
        created: Instant,
        id: &str,
    ) -> Result<Snapshot, SearchError> {
        let mut cursors = Vec::new();
        for offset in (binding.limit..page.matches.len()).step_by(binding.limit) {
            let nonce = uuid::Uuid::new_v4();
            let mut encoded = nonce.as_bytes()[..12].to_vec();
            let encrypted = self
                .cipher
                .encrypt(
                    Nonce::from_slice(&encoded),
                    format!("{id}.{offset}").as_bytes(),
                )
                .map_err(|_| error("search_result_invalid"))?;
            encoded.extend(encrypted);
            cursors.push(URL_SAFE_NO_PAD.encode(encoded));
        }
        // Conservative heap accounting includes capacity, scope strings, cursor storage and map overhead.
        let bytes = snapshot_bytes(&page)
            + cursors.iter().map(|s| s.capacity()).sum::<usize>()
            + cursors.capacity() * std::mem::size_of::<String>()
            + binding.run.capacity()
            + binding.project.capacity()
            + binding.conversation.capacity()
            + binding.path.capacity()
            + binding.query.capacity()
            + binding.mode.capacity()
            + binding.root.as_os_str().len() * 4
            + 1024;
        Ok(Snapshot {
            binding,
            page,
            created,
            used: 0,
            bytes,
            cursors,
        })
    }
    fn cached(
        &self,
        binding: &Binding,
        cursor: &str,
        cancellation: &Cancellation,
        now: Instant,
    ) -> Result<SearchPage, SearchError> {
        let encoded = URL_SAFE_NO_PAD
            .decode(cursor)
            .map_err(|_| error("invalid_search_cursor"))?;
        if encoded.len() < 28 || cursor.len() > 256 {
            return Err(error("invalid_search_cursor"));
        }
        let decoded = self
            .cipher
            .decrypt(Nonce::from_slice(&encoded[..12]), &encoded[12..])
            .map_err(|_| error("invalid_search_cursor"))?;
        let decoded = std::str::from_utf8(&decoded).map_err(|_| error("invalid_search_cursor"))?;
        let (id, offset) = decoded
            .split_once('.')
            .ok_or_else(|| error("invalid_search_cursor"))?;
        let offset: usize = offset.parse().map_err(|_| error("invalid_search_cursor"))?;
        let mut state = self.state.lock().unwrap();
        cancellation.check()?;
        state
            .snapshots
            .retain(|_, s| now.duration_since(s.created) < TTL);
        state.clock += 1;
        let used = state.clock;
        let snapshot = state
            .snapshots
            .get_mut(id)
            .ok_or_else(|| error("search_cursor_expired"))?;
        if snapshot.binding != *binding
            || offset == 0
            || !offset.is_multiple_of(binding.limit)
            || offset >= snapshot.page.matches.len()
        {
            return Err(error("invalid_search_cursor"));
        }
        snapshot.used = used;
        Ok(page_at(snapshot, offset))
    }
}
fn snapshot_bytes(page: &SearchPage) -> usize {
    std::mem::size_of::<Snapshot>()
        + page.matches.capacity() * std::mem::size_of::<String>()
        + page.matches.iter().map(String::capacity).sum::<usize>()
        + page.stop_reason.as_ref().map_or(0, String::capacity)
}

fn page_at(snapshot: &Snapshot, offset: usize) -> SearchPage {
    let page = &snapshot.page;
    let limit = snapshot.binding.limit;
    let end = offset.saturating_add(limit).min(page.matches.len());
    SearchPage {
        matches: page.matches[offset..end].to_vec(),
        next_cursor: snapshot.cursors.get(offset / limit).cloned(),
        inspected_files: page.inspected_files,
        complete: page.complete,
        truncated: page.truncated,
        stop_reason: page.stop_reason.clone(),
    }
}

trait Clock {
    fn elapsed(&self) -> Duration;
}
impl Clock for Instant {
    fn elapsed(&self) -> Duration {
        Instant::elapsed(self)
    }
}
trait Node {
    fn is_dir(&self) -> bool;
    fn len(&self) -> u64;
    fn entries(&self, visitor: &mut dyn FnMut(String) -> bool) -> Result<(), SearchError>;
    fn read(&self, buffer: &mut [u8]) -> Result<usize, SearchError>;
}
impl Node for VerifiedReadHandle {
    fn is_dir(&self) -> bool {
        self.is_dir()
    }
    fn len(&self) -> u64 {
        self.len()
    }
    fn entries(&self, visitor: &mut dyn FnMut(String) -> bool) -> Result<(), SearchError> {
        self.visit_entries(visitor)
    }
    fn read(&self, buffer: &mut [u8]) -> Result<usize, SearchError> {
        self.read_chunk(buffer)
    }
}
trait Walker {
    type Handle: Node;
    fn open(&self, path: &str) -> Result<Self::Handle, SearchError>;
}
struct Disk(AuthorizedRoot);
impl Walker for Disk {
    type Handle = VerifiedReadHandle;
    fn open(&self, path: &str) -> Result<Self::Handle, SearchError> {
        FileAccessPolicy.open_read(&self.0, &FileAccessPolicy.validate_relative(path)?)
    }
}
fn filtered(error: &str) -> bool {
    [
        "forbidden_path:",
        "protected_path:",
        "path_escape:",
        "reparse_point_forbidden:",
        "hard_link_forbidden:",
    ]
    .iter()
    .any(|prefix| error.starts_with(prefix))
}

fn scan(
    walker: &impl Walker,
    request: &SearchRequest,
    cancellation: &Cancellation,
    budget: SearchBudget,
    clock: &impl Clock,
) -> Result<SearchPage, SearchError> {
    let mut page = SearchPage {
        matches: Vec::new(),
        inspected_files: 0,
        next_cursor: None,
        complete: true,
        truncated: false,
        stop_reason: None,
    };
    let mut pending = vec![(request.path.clone(), 0usize)];
    let mut entries = 0;
    let mut bytes = 0;
    let mut memory = 0;
    let mut stop = None;
    let query_lower = request.query.to_ascii_lowercase();
    while let Some((path, depth)) = pending.pop() {
        cancellation.check()?;
        if clock.elapsed() >= budget.duration {
            stop = Some("deadline");
            break;
        }
        let file = match walker.open(&path) {
            Ok(file) => file,
            Err(e) if filtered(&e) && path != request.path => continue,
            Err(e) if path == request.path => return Err(e),
            Err(_) => return Err(error("search_read_failed")),
        };
        if file.is_dir() {
            if depth >= budget.depth {
                stop = Some("depth");
                break;
            }
            let mut children = Vec::new();
            file.entries(&mut |name| {
                if cancellation.check().is_err() {
                    return false;
                }
                if clock.elapsed() >= budget.duration {
                    stop = Some("deadline");
                    return false;
                }
                if entries >= budget.entries {
                    stop = Some("entries");
                    return false;
                }
                entries += 1;
                let child = if path == "." {
                    name
                } else {
                    format!("{path}/{name}")
                };
                if FileAccessPolicy.validate_relative(&child).is_ok() {
                    memory += child.len() + std::mem::size_of::<String>();
                    if memory >= CACHE_BYTES / 2 {
                        stop = Some("bytes");
                        return false;
                    }
                    children.push(child);
                }
                if entries >= budget.entries {
                    stop = Some("entries");
                    return false;
                }
                true
            })
            .map_err(|_| error("search_read_failed"))?;
            cancellation.check()?;
            if stop.is_some() {
                break;
            }
            children.sort();
            pending.extend(children.into_iter().rev().map(|child| (child, depth + 1)));
            continue;
        }
        if page.inspected_files >= budget.files {
            stop = Some("files");
            break;
        }
        page.inspected_files += 1;
        let found = if request.mode == "name" {
            path.to_ascii_lowercase().contains(&query_lower)
        } else {
            if file.len() > budget.file_bytes as u64 {
                stop = Some("bytes");
                break;
            }
            let mut content = Vec::new();
            let mut buffer = [0u8; 16384];
            loop {
                cancellation.check()?;
                if clock.elapsed() >= budget.duration {
                    stop = Some("deadline");
                    break;
                }
                let remaining = budget
                    .bytes
                    .saturating_sub(bytes)
                    .min(budget.file_bytes.saturating_sub(content.len()));
                if remaining == 0 {
                    if content.len() < file.len() as usize {
                        stop = Some("bytes");
                    }
                    break;
                }
                let size = remaining.min(buffer.len());
                let count = file
                    .read(&mut buffer[..size])
                    .map_err(|_| error("search_read_failed"))?;
                bytes += count;
                if count == 0 {
                    break;
                }
                content.extend_from_slice(&buffer[..count]);
            }
            if stop.is_some() {
                break;
            }
            std::str::from_utf8(&content).is_ok_and(|text| text.contains(&request.query))
        };
        if found {
            page.matches.push(path);
        }
    }
    cancellation.check()?;
    if stop.is_none() && clock.elapsed() >= budget.duration {
        stop = Some("deadline");
    }
    if let Some(reason) = stop {
        page.complete = false;
        page.truncated = true;
        page.stop_reason = Some(reason.into());
    }
    page.matches.sort();
    Ok(page)
}

#[cfg(test)]
pub fn search_for_test(
    root: AuthorizedRoot,
    arguments: &serde_json::Value,
) -> Result<String, String> {
    let request = SearchRequest::parse(arguments)?;
    if request.cursor.is_some() {
        return Err(error("invalid_search_cursor"));
    }
    serde_json::to_string(&scan(
        &Disk(root),
        &request,
        &Cancellation::default(),
        SearchBudget::default(),
        &Instant::now(),
    )?)
    .map_err(|_| error("search_result_invalid"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    struct Synthetic {
        entries: usize,
        size: usize,
        binary: bool,
        fail_read: bool,
        listed: Cell<usize>,
        read: Cell<usize>,
        opens: Cell<usize>,
        cancellation: Option<Cancellation>,
    }
    impl Synthetic {
        fn new(entries: usize, size: usize) -> Self {
            Self {
                entries,
                size,
                binary: false,
                fail_read: false,
                listed: Cell::new(0),
                read: Cell::new(0),
                opens: Cell::new(0),
                cancellation: None,
            }
        }
    }
    struct SyntheticNode<'a> {
        source: &'a Synthetic,
        directory: bool,
        position: Cell<usize>,
    }
    impl Node for SyntheticNode<'_> {
        fn is_dir(&self) -> bool {
            self.directory
        }
        fn len(&self) -> u64 {
            self.source.size as u64
        }
        fn entries(&self, visitor: &mut dyn FnMut(String) -> bool) -> Result<(), String> {
            for i in 0..self.source.entries {
                self.source.listed.set(self.source.listed.get() + 1);
                if !visitor(format!("{i:06}.txt")) {
                    break;
                }
            }
            Ok(())
        }
        fn read(&self, buffer: &mut [u8]) -> Result<usize, String> {
            if self.source.fail_read {
                return Err(error("file_read_failed"));
            }
            if let Some(cancellation) = &self.source.cancellation {
                cancellation.cancel(1);
            }
            let size = buffer.len().min(self.source.size - self.position.get());
            buffer[..size].fill(if self.source.binary { 255 } else { b'a' });
            self.position.set(self.position.get() + size);
            self.source.read.set(self.source.read.get() + size);
            Ok(size)
        }
    }
    impl<'a> Walker for &'a Synthetic {
        type Handle = SyntheticNode<'a>;
        fn open(&self, path: &str) -> Result<Self::Handle, String> {
            self.opens.set(self.opens.get() + 1);
            Ok(SyntheticNode {
                source: self,
                directory: path == ".",
                position: Cell::new(0),
            })
        }
    }
    struct FixedClock;
    impl Clock for FixedClock {
        fn elapsed(&self) -> Duration {
            Duration::ZERO
        }
    }
    struct StepClock(Cell<u64>);
    impl Clock for StepClock {
        fn elapsed(&self) -> Duration {
            let n = self.0.get();
            self.0.set(n + 1);
            Duration::from_millis(n)
        }
    }
    fn request(mode: &str) -> SearchRequest {
        SearchRequest {
            path: ".".into(),
            query: "missing".into(),
            mode: mode.into(),
            limit: 2,
            cursor: None,
        }
    }

    #[test]
    fn ended_and_shutdown_generations_reject_late_admission() {
        let service = FileSearchService::default();
        let ended = service.begin_core_generation();
        service.end_core_generation(ended);
        assert!(service
            .admit_core_event(ended, CoreEventAction::Observe)
            .is_err());
        let shutdown = service.begin_core_generation();
        service.invalidate_all();
        assert!(service
            .admit_core_event(shutdown, CoreEventAction::Search("late"))
            .is_err());
        assert!(service
            .admit_core_event(shutdown, CoreEventAction::CancelRun("late"))
            .is_err());
        let state = service.state.lock().unwrap();
        assert!(state.tasks.is_empty());
        assert!(state.cancelled_runs.is_empty());
    }

    #[test]
    fn stale_reader_cannot_admit_search_after_replacement() {
        let service = FileSearchService::default();
        let old = service.begin_core_generation();
        // A has passed its reader running check; pause before dispatch admission.
        let current = service.begin_core_generation();
        let task = service
            .admit_core_event(current, CoreEventAction::Search("current"))
            .unwrap()
            .unwrap();
        assert!(service
            .admit_core_event(old, CoreEventAction::Search("late-old"))
            .is_err());
        assert_eq!(service.state.lock().unwrap().tasks.len(), 1);
        assert!(task.cancellation.check().is_ok());
    }

    #[test]
    fn stale_reader_cannot_admit_terminal_after_replacement() {
        let service = FileSearchService::default();
        let old = service.begin_core_generation();
        // A passed its running check before B began, then resumes with B's run ID.
        let current = service.begin_core_generation();
        let task = service
            .admit_core_event(current, CoreEventAction::Search("same-run"))
            .unwrap()
            .unwrap();
        assert!(service
            .admit_core_event(old, CoreEventAction::CancelRun("same-run"))
            .is_err());
        assert!(service.state.lock().unwrap().cancelled_runs.is_empty());
        assert!(task.cancellation.check().is_ok());
        assert!(service
            .admit_core_event(current, CoreEventAction::CancelRun("same-run"))
            .is_ok());
        assert!(task.cancellation.check().is_err());
    }

    #[test]
    fn file_search_100000_entries_stop_incrementally_before_files() {
        let source = Synthetic::new(100000, 1);
        let page = scan(
            &&source,
            &request("name"),
            &Cancellation::default(),
            SearchBudget::default(),
            &FixedClock,
        )
        .unwrap();
        assert_eq!(page.stop_reason.as_deref(), Some("entries"));
        assert!(page.truncated && !page.complete);
        assert!(source.listed.get() <= 20000);
        assert_eq!(source.read.get(), 0);
    }
    #[test]
    fn file_search_sparse_matches_obey_file_and_total_byte_ceilings() {
        let source = Synthetic::new(10001, 0);
        let page = scan(
            &&source,
            &request("content"),
            &Cancellation::default(),
            SearchBudget::default(),
            &FixedClock,
        )
        .unwrap();
        assert_eq!(page.inspected_files, 10000);
        assert_eq!(page.stop_reason.as_deref(), Some("files"));
        let source = Synthetic::new(100, 1048576);
        let page = scan(
            &&source,
            &request("content"),
            &Cancellation::default(),
            SearchBudget::default(),
            &FixedClock,
        )
        .unwrap();
        assert_eq!(source.read.get(), 67108864);
        assert_eq!(page.stop_reason.as_deref(), Some("bytes"));
        assert!(page.matches.is_empty() && !page.complete);
    }
    #[test]
    fn file_search_deadline_applies_to_enumeration_and_read_errors_are_not_empty_success() {
        let source = Synthetic::new(100000, 1);
        let page = scan(
            &&source,
            &request("name"),
            &Cancellation::default(),
            SearchBudget::default(),
            &StepClock(Cell::new(0)),
        )
        .unwrap();
        assert_eq!(page.stop_reason.as_deref(), Some("deadline"));
        assert!(source.listed.get() <= 2000);
        let mut source = Synthetic::new(1, 8);
        source.fail_read = true;
        assert!(scan(
            &&source,
            &request("content"),
            &Cancellation::default(),
            SearchBudget::default(),
            &FixedClock
        )
        .unwrap_err()
        .starts_with("search_read_failed:"));
        source.fail_read = false;
        source.binary = true;
        let page = scan(
            &&source,
            &request("content"),
            &Cancellation::default(),
            SearchBudget::default(),
            &FixedClock,
        )
        .unwrap();
        assert!(page.complete && page.matches.is_empty());
    }
    #[test]
    fn file_search_deadline_after_last_io_is_not_complete() {
        struct AfterIo(Cell<bool>);
        impl Clock for AfterIo {
            fn elapsed(&self) -> Duration {
                if self.0.replace(true) {
                    Duration::from_secs(3)
                } else {
                    Duration::ZERO
                }
            }
        }
        let source = Synthetic::new(0, 0);
        let page = scan(
            &&source,
            &request("name"),
            &Cancellation::default(),
            SearchBudget::default(),
            &AfterIo(Cell::new(false)),
        )
        .unwrap();
        assert_eq!(page.stop_reason.as_deref(), Some("deadline"));
        assert!(!page.complete);
    }
    #[test]
    fn file_search_cancellation_during_read_discards_matches() {
        let cancellation = Cancellation::default();
        let mut source = Synthetic::new(100, 1048576);
        source.cancellation = Some(cancellation.clone());
        assert!(scan(
            &&source,
            &request("content"),
            &cancellation,
            SearchBudget::default(),
            &FixedClock
        )
        .unwrap_err()
        .starts_with("cancelled:"));
        assert!(source.read.get() <= 16384);
    }
    #[test]
    fn file_search_old_core_exit_does_not_cancel_new_core_tasks() {
        let service = FileSearchService::default();
        let old_generation = service.begin_core_generation();
        let old_task = service.register("old");
        let new_generation = service.begin_core_generation();
        let new_task = service.register("new");
        service.end_core_generation(old_generation);
        assert!(old_task.cancellation.check().is_err());
        assert!(new_task.cancellation.check().is_ok());
        service.end_core_generation(new_generation);
        assert!(new_task.cancellation.check().is_err());
    }
    #[test]
    fn file_search_scope_change_blocks_late_bind_and_discards_pending_scope() {
        let service = FileSearchService::default();
        let task = service.register("run");
        service
            .bind_project(&task, "project", "conversation")
            .unwrap();
        let change = service.changing_conversation("conversation");
        assert!(task.cancellation.check().is_err());
        let late = service.register("other");
        assert!(service
            .bind_project(&late, "project", "conversation")
            .is_err());
        drop(change);
        assert!(late.cancellation.check().is_err());
        let fresh = service.register("fresh");
        service
            .bind_project(&fresh, "project", "conversation")
            .unwrap();
    }
    #[test]
    fn file_search_late_dispatch_after_cancellation_is_rejected() {
        let service = FileSearchService::default();
        service.cancel_run("ended");
        let late = service.register("ended");
        assert!(service
            .bind_project(&late, "project", "conversation")
            .is_err());
    }

    #[test]
    fn file_search_terminal_and_authorization_cancellation_prevent_publication() {
        let service = FileSearchService::default();
        let task = service.register("run");
        service
            .bind_project(&task, "project", "conversation")
            .unwrap();
        service.invalidate_project("project");
        let replies = Cell::new(0);
        assert!(service
            .commit(&task, || {
                replies.set(replies.get() + 1);
                Ok(())
            })
            .is_err());
        assert_eq!(replies.get(), 0);
        let task = service.register("run");
        service.cancel_run("run");
        assert!(service
            .bind_project(&task, "project", "conversation")
            .is_err());
        assert!(service
            .commit(&task, || {
                replies.set(replies.get() + 1);
                Ok(())
            })
            .is_err());
        let task = service.register("other");
        service.invalidate_all();
        assert!(service
            .commit(&task, || {
                replies.set(replies.get() + 1);
                Ok(())
            })
            .is_err());
        assert_eq!(replies.get(), 0);
    }
    fn binding() -> Binding {
        Binding {
            run: "run".into(),
            project: "project".into(),
            conversation: "conversation".into(),
            generation: 1,
            policy: 1,
            root: "root".into(),
            path: ".".into(),
            query: "match".into(),
            mode: "name".into(),
            limit: 2,
        }
    }
    #[test]
    fn file_search_cursor_cannot_be_forged_and_expiration_is_explicit() {
        let service = FileSearchService::default();
        let now = Instant::now();
        let id = uuid::Uuid::new_v4().simple().to_string();
        let page = SearchPage {
            matches: (0..8).map(|i| format!("{i}.txt")).collect(),
            inspected_files: 8,
            next_cursor: None,
            complete: false,
            truncated: true,
            stop_reason: Some("files".into()),
        };
        service.state.lock().unwrap().snapshots.insert(
            id.clone(),
            service.snapshot(binding(), page, now, &id).unwrap(),
        );
        assert!(
            service
                .cached(
                    &binding(),
                    &format!("{id}.2"),
                    &Cancellation::default(),
                    now
                )
                .is_err(),
            "a numeric position appended to a known snapshot is not a Host-issued cursor"
        );
    }
    fn matches_page(count: usize) -> SearchPage {
        SearchPage {
            matches: (0..count).map(|i| format!("{i:04}.txt")).collect(),
            inspected_files: count,
            next_cursor: None,
            complete: false,
            truncated: true,
            stop_reason: Some("files".into()),
        }
    }
    #[tokio::test]
    async fn file_search_cached_pages_never_read_again_and_are_idempotent() {
        use std::sync::atomic::AtomicUsize;
        let service = FileSearchService::default();
        let task = service.register("run");
        let reads = Arc::new(AtomicUsize::new(0));
        let counter = reads.clone();
        let first = service
            .search_with(binding(), None, &task, move |_| {
                counter.fetch_add(50, Ordering::SeqCst);
                Ok(matches_page(5))
            })
            .await
            .unwrap();
        let cursor = first.next_cursor.unwrap();
        assert!(cursor.len() <= 256);
        let second = service
            .search_with(binding(), Some(cursor.clone()), &task, |_| {
                panic!("cached page rescanned disk")
            })
            .await
            .unwrap();
        let repeated = service
            .search_with(binding(), Some(cursor), &task, |_| {
                panic!("repeated page rescanned disk")
            })
            .await
            .unwrap();
        assert_eq!(
            serde_json::to_value(&second).unwrap(),
            serde_json::to_value(repeated).unwrap()
        );
        assert_eq!(reads.load(Ordering::SeqCst), 50);
        let last = service
            .search_with(binding(), second.next_cursor, &task, |_| {
                panic!("last page rescanned disk")
            })
            .await
            .unwrap();
        assert_eq!(last.matches, ["0004.txt"]);
        assert!(last.next_cursor.is_none() && last.truncated && !last.complete);
        assert_eq!(last.stop_reason.as_deref(), Some("files"));
    }
    #[test]
    fn file_search_cached_scope_ttl_and_lru_limits() {
        let service = FileSearchService::default();
        let cancellation = Cancellation::default();
        let now = Instant::now();
        let first = service
            .cache(binding(), matches_page(5), &cancellation, now)
            .unwrap()
            .next_cursor
            .unwrap();
        for change in 0..8 {
            let mut other = binding();
            match change {
                0 => other.run = "other".into(),
                1 => other.project = "other".into(),
                2 => other.conversation = "other".into(),
                3 => other.generation += 1,
                4 => other.policy += 1,
                5 => other.path = "subdir".into(),
                6 => other.mode = "content".into(),
                _ => other.query = "other".into(),
            }
            assert!(service
                .cached(&other, &first, &cancellation, now)
                .unwrap_err()
                .starts_with("invalid_search_cursor:"));
        }
        let second = service
            .cache(binding(), matches_page(5), &cancellation, now)
            .unwrap()
            .next_cursor
            .unwrap();
        for _ in 0..30 {
            service
                .cache(binding(), matches_page(5), &cancellation, now)
                .unwrap();
        }
        service
            .cached(&binding(), &first, &cancellation, now)
            .unwrap();
        service
            .cache(binding(), matches_page(5), &cancellation, now)
            .unwrap();
        assert!(service
            .cached(&binding(), &second, &cancellation, now)
            .unwrap_err()
            .starts_with("search_cursor_expired:"));
        assert!(service
            .cached(&binding(), &first, &cancellation, now + TTL)
            .unwrap_err()
            .starts_with("search_cursor_expired:"));
        let mut large = matches_page(5);
        large.matches = (0..5)
            .map(|i| format!("{i}{}", "x".repeat(250000)))
            .collect();
        for _ in 0..32 {
            service
                .cache(binding(), large.clone(), &cancellation, now + TTL)
                .unwrap();
        }
        let state = service.state.lock().unwrap();
        assert!(state.snapshots.len() < 32);
        assert!(state.snapshots.values().map(|s| s.bytes).sum::<usize>() <= CACHE_BYTES);
    }
    #[tokio::test]
    async fn file_search_two_workers_eight_queued_and_cancelled_late_results() {
        use std::sync::{atomic::AtomicUsize, Condvar};
        let service = FileSearchService::default();
        let started = Arc::new(AtomicUsize::new(0));
        let gate = Arc::new((Mutex::new(false), Condvar::new()));
        let mut jobs = Vec::new();
        for i in 0..10 {
            let service = service.clone();
            let started = started.clone();
            let gate = gate.clone();
            let task = service.register(&format!("run{i}"));
            jobs.push(tokio::spawn(async move {
                service
                    .search_with(binding(), None, &task, move |_| {
                        started.fetch_add(1, Ordering::SeqCst);
                        let (lock, wake) = &*gate;
                        let (released, _) = wake
                            .wait_timeout_while(
                                lock.lock().unwrap(),
                                Duration::from_secs(10),
                                |ready| !*ready,
                            )
                            .unwrap();
                        assert!(*released, "test did not release blocked worker");
                        Ok(matches_page(5))
                    })
                    .await
            }));
        }
        tokio::time::timeout(Duration::from_secs(3), async {
            while service.admission.available_permits() != 0 || started.load(Ordering::SeqCst) != 2
            {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        let extra = service.register("extra");
        let result = service
            .search_with(binding(), None, &extra, |_| panic!("eleventh scan started"))
            .await;
        assert!(result.unwrap_err().starts_with("search_busy:"));
        assert_eq!(started.load(Ordering::SeqCst), 2);
        service.invalidate_all();
        *gate.0.lock().unwrap() = true;
        gate.1.notify_all();
        let mut successful_replies_after_cancel = 0;
        for job in jobs {
            if job.await.unwrap().is_ok() {
                successful_replies_after_cancel += 1;
            }
        }
        assert_eq!(successful_replies_after_cancel, 0);
        assert_eq!(started.load(Ordering::SeqCst), 2);
        assert_eq!(service.slots.available_permits(), 2);
        assert_eq!(service.admission.available_permits(), 10);
    }
    #[test]
    fn file_search_depth_is_bounded_for_directory_only_trees() {
        struct Deep;
        impl Node for Deep {
            fn is_dir(&self) -> bool {
                true
            }
            fn len(&self) -> u64 {
                0
            }
            fn entries(&self, visitor: &mut dyn FnMut(String) -> bool) -> Result<(), String> {
                visitor("d".into());
                Ok(())
            }
            fn read(&self, _: &mut [u8]) -> Result<usize, String> {
                panic!("directory read")
            }
        }
        impl Walker for Deep {
            type Handle = Deep;
            fn open(&self, _: &str) -> Result<Deep, String> {
                Ok(Deep)
            }
        }
        let page = scan(
            &Deep,
            &request("name"),
            &Cancellation::default(),
            SearchBudget::default(),
            &FixedClock,
        )
        .unwrap();
        assert_eq!(page.stop_reason.as_deref(), Some("depth"));
        assert_eq!(page.inspected_files, 0);
        assert!(page.truncated && !page.complete);
    }
}
