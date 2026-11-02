//! Recovery must release an unconfirmed preparation without killing unknown work.
use std::future::Future;

pub async fn recover<F, A, R>(shutdown_started: bool, prepared: bool, abort: A, restart: R)
where
    F: Future<Output = Result<(), String>>,
    A: FnOnce() -> F,
    R: FnOnce(),
{
    // Abort must run even without a confirmed prepare; only restarting requires
    // confirmed idle ownership. A closed stdin bypasses abort entirely.
    if shutdown_started || (abort().await.is_err() && prepared) {
        restart();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        cell::Cell,
        future::ready,
        pin::pin,
        task::{Context, Poll, Waker},
    };

    fn complete(future: impl Future<Output = ()>) {
        assert!(matches!(
            pin!(future).poll(&mut Context::from_waker(Waker::noop())),
            Poll::Ready(())
        ));
    }

    #[test]
    fn lost_prepare_ack_still_releases_its_admission_gate() {
        // The Core prepared successfully; only its response was lost in transit.
        let admission_closed = Cell::new(true);
        complete(recover(
            false,
            false,
            || {
                admission_closed.set(false);
                ready(Ok(()))
            },
            || panic!("An unconfirmed prepare must not kill potentially active work"),
        ));
        assert!(
            !admission_closed.get(),
            "A lost ACK must not leave chat locked"
        );
    }

    #[test]
    fn unavailable_abort_cannot_terminate_work_without_confirmed_idle_preparation() {
        let running_work = Cell::new(1);
        complete(recover(
            false,
            false,
            || ready(Err("transport unavailable".into())),
            || running_work.set(0),
        ));
        assert_eq!(running_work.get(), 1);
    }

    #[test]
    fn failed_abort_after_confirmed_preparation_recovers_the_owned_idle_core() {
        let admission_closed = Cell::new(true);
        complete(recover(
            false,
            true,
            || ready(Err("transport unavailable".into())),
            || admission_closed.set(false),
        ));
        assert!(!admission_closed.get());
    }

    #[test]
    fn closed_stdin_recovers_by_restart_instead_of_sending_to_a_dead_queue() {
        let restarted = Cell::new(false);
        complete(recover(
            true,
            true,
            || -> std::future::Ready<Result<(), String>> { panic!("stdin is already closed") },
            || restarted.set(true),
        ));
        assert!(restarted.get());
    }
}
