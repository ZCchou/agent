pub(crate) mod commands;

mod error;
mod manager;
mod model;
mod repository;

pub use manager::SettingsManager;
#[cfg(test)]
pub(crate) use model::invocation_routes_for_profile;
pub use model::{AppSettings, SearchBackendSettings, WebSearchSettings};

pub use repository::TauriStoreSettingsRepository;
