use keyring::Entry;

const SERVICE: &str = "agent Model Providers";

#[derive(Default)]
pub struct CredentialStore;

impl CredentialStore {
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
