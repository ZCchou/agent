//! Bounded FIFO used by the Windows Core stdin writer.
use std::{
    collections::VecDeque,
    io::Write,
    sync::{Condvar, Mutex},
};

#[derive(Default)]
struct Message {
    id: String,
    bytes: Vec<u8>,
}
#[derive(Default)]
struct State {
    messages: VecDeque<Message>,
    bytes: usize,
    count: usize,
    closed: bool,
}
pub(super) struct WriteQueue {
    state: Mutex<State>,
    wake: Condvar,
    max_count: usize,
    max_bytes: usize,
}
impl WriteQueue {
    pub(super) fn new(max_count: usize, max_bytes: usize) -> Self {
        Self {
            state: Mutex::default(),
            wake: Condvar::new(),
            max_count,
            max_bytes,
        }
    }
    pub(super) fn push(&self, id: &str, bytes: &[u8]) -> Result<(), String> {
        let mut state = self.state.lock().unwrap();
        if state.closed {
            return Err("core_transport_closed: Core writer is closed".into());
        }
        if state.count >= self.max_count || bytes.len() > self.max_bytes.saturating_sub(state.bytes)
        {
            return Err("core_transport_busy: Core writer queue is full".into());
        }
        state.messages.push_back(Message {
            id: id.into(),
            bytes: bytes.to_vec(),
        });
        state.count += 1;
        state.bytes += bytes.len();
        self.wake.notify_one();
        Ok(())
    }
    fn pop(&self) -> Option<Message> {
        let mut state = self.state.lock().unwrap();
        loop {
            if state.closed {
                return None;
            }
            if let Some(message) = state.messages.pop_front() {
                return Some(message);
            }
            state = self.wake.wait(state).unwrap();
        }
    }
    fn finish(&self, bytes: usize) {
        let mut state = self.state.lock().unwrap();
        state.count -= 1;
        state.bytes -= bytes;
    }
    pub(super) fn close(&self) {
        let mut state = self.state.lock().unwrap();
        state.closed = true;
        while let Some(message) = state.messages.pop_front() {
            state.count -= 1;
            state.bytes -= message.bytes.len();
        }
        self.wake.notify_all();
    }
    pub(super) fn cancel(&self, id: &str) {
        let mut state = self.state.lock().unwrap();
        let mut removed_count = 0;
        let mut removed_bytes = 0;
        state.messages.retain(|message| {
            if message.id == id {
                removed_count += 1;
                removed_bytes += message.bytes.len();
                false
            } else {
                true
            }
        });
        state.count -= removed_count;
        state.bytes -= removed_bytes;
    }
    pub(super) fn run(&self, mut writer: impl Write, failed: impl FnOnce()) {
        while let Some(message) = self.pop() {
            // No authorization, manager, or queue mutex is retained during pipe I/O.
            let result = writer.write_all(&message.bytes);
            self.finish(message.bytes.len());
            if result.is_err() {
                self.close();
                failed();
                return;
            }
        }
    }
}
#[cfg(test)]
pub(super) fn in_flight_count(queue: &WriteQueue) -> usize {
    let state = queue.state.lock().unwrap();
    state.count - state.messages.len()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn core_transport_queue_bounds_count_and_bytes() {
        let count = WriteQueue::new(2, 100);
        count.push("id", b"one").unwrap();
        count.push("id", b"two").unwrap();
        assert!(count.push("id", b"three").is_err());
        let bytes = WriteQueue::new(100, 8);
        bytes.push("id", b"12345").unwrap();
        assert!(bytes.push("id", b"6789").is_err());
        assert_eq!(bytes.state.lock().unwrap().bytes, 5);
    }
    #[test]
    fn core_transport_queue_counts_inflight_and_close_wakes_waiter() {
        let queue = WriteQueue::new(1, 8);
        queue.push("id", b"12345678").unwrap();
        let message = queue.pop().unwrap();
        assert!(queue.push("id", b"x").is_err());
        queue.finish(message.bytes.len());
        queue.push("id", b"x").unwrap();
        queue.close();
        assert!(queue.pop().is_none());
        assert!(queue.push("id", b"x").is_err());
        assert_eq!(queue.state.lock().unwrap().bytes, 0);
        let queue = std::sync::Arc::new(WriteQueue::new(2, 8));
        let other = queue.clone();
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            tx.send(other.pop()).unwrap();
        });
        queue.close();
        assert!(rx
            .recv_timeout(std::time::Duration::from_secs(1))
            .unwrap()
            .is_none());
    }
    #[test]
    fn core_transport_queue_failure_discards_pending_without_replay() {
        struct Broken;
        impl Write for Broken {
            fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
                Err(std::io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let queue = WriteQueue::new(2, 8);
        queue.push("id", b"one").unwrap();
        queue.push("id", b"two").unwrap();
        let failed = std::cell::Cell::new(0);
        queue.run(Broken, || failed.set(failed.get() + 1));
        assert_eq!(failed.get(), 1);
        assert!(queue.push("id", b"again").is_err());
        assert_eq!(queue.state.lock().unwrap().count, 0);
    }
    #[test]
    fn core_transport_queue_cancels_unsent_requests() {
        let queue = WriteQueue::new(2, 8);
        queue.push("one", b"one").unwrap();
        queue.cancel("one");
        assert_eq!(queue.state.lock().unwrap().count, 0);
    }
}
