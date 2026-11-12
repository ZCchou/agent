use keyring::Entry;

const SERVICE: &str = "io.github.zcchou.agent.airworthiness Model Providers";
const LEGACY_SERVICE: &str = "agent Model Providers";

fn transfer_if_missing(
    read_target: impl FnOnce() -> Result<Option<String>, String>,
    read_source: impl FnOnce() -> Result<Option<String>, String>,
    write_target: impl FnOnce(&str) -> Result<(), String>,
) -> Result<(), String> {
    if read_target()?.is_none() {
        if let Some(secret) = read_source()? {
            write_target(&secret)?;
        }
    }
    Ok(())
}

#[derive(Default)]
pub struct CredentialStore;

impl CredentialStore {
    pub(crate) fn get_legacy(&self, id: &str) -> Result<Option<String>, String> {
        let entry =
            Entry::new(LEGACY_SERVICE, id).map_err(|_| "Credential Manager is unavailable")?;
        match entry.get_password() {
            Ok(secret) => Ok(Some(secret)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(_) => Err("Failed to read a legacy credential".into()),
        }
    }
    pub fn copy_legacy_if_missing(&self, source_id: &str, target_id: &str) -> Result<(), String> {
        transfer_if_missing(
            || self.get(target_id),
            || {
                let entry = Entry::new(LEGACY_SERVICE, source_id)
                    .map_err(|_| "Credential Manager is unavailable")?;
                match entry.get_password() {
                    Ok(secret) => Ok(Some(secret)),
                    Err(keyring::Error::NoEntry) => Ok(None),
                    Err(_) => Err("Failed to read a legacy local provider credential".into()),
                }
            },
            |secret| self.set(target_id, secret),
        )
    }
    fn entry(provider_id: &str) -> Result<Entry, String> {
        Entry::new(SERVICE, provider_id)
            .map_err(|_| "Windows Credential Manager is unavailable".to_string())
    }

    pub fn set(&self, provider_id: &str, secret: &str) -> Result<(), String> {
        Self::entry(provider_id)?
            .set_password(secret)
            .map_err(|_| "Failed to save the provider credential".to_string())
    }

    pub fn get(&self, provider_id: &str) -> Result<Option<String>, String> {
        match Self::entry(provider_id)?.get_password() {
            Ok(secret) => Ok(Some(secret)),
            Err(keyring::Error::NoEntry) => Ok(None),
            Err(_) => Err("Failed to read the provider credential".to_string()),
        }
    }

    pub fn delete(&self, provider_id: &str) -> Result<(), String> {
        match Self::entry(provider_id)?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(_) => Err("Failed to delete the provider credential".to_string()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn migration_preserves_existing_credentials_and_only_writes_new_namespace() {
        transfer_if_missing(
            || Ok(Some("existing".into())),
            || panic!("Existing target must win"),
            |_| panic!("Must preserve target"),
        )
        .unwrap();
        let copied = std::cell::RefCell::new(String::new());
        transfer_if_missing(
            || Ok(None),
            || Ok(Some("legacy-test-value".into())),
            |secret| {
                *copied.borrow_mut() = secret.into();
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(*copied.borrow(), "legacy-test-value");
    }
}
