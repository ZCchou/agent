//! One explicit profile root for Host and Core; default installs keep their existing paths.
use std::{
    ffi::OsString,
    path::{Component, PathBuf},
};
use tauri::{AppHandle, Manager};

fn configured_root(default: PathBuf, configured: Option<OsString>) -> Result<PathBuf, String> {
    let Some(value) = configured else {
        return Ok(default);
    };
    let path = PathBuf::from(value);
    if path.as_os_str().is_empty()
        || !path.is_absolute()
        || path
            .components()
            .any(|p| matches!(p, Component::ParentDir | Component::CurDir))
    {
        return Err(
            "AGENT_PROFILE_DIR must be an absolute profile directory without traversal".into(),
        );
    }
    if path.file_name().and_then(|name| name.to_str())
        != Some(crate::program_update::APPLICATION_ID)
    {
        return Err("AGENT_PROFILE_DIR must retain the application identity directory name".into());
    }
    for ancestor in path.ancestors() {
        match std::fs::symlink_metadata(ancestor) {
            Ok(metadata) => {
                #[cfg(windows)]
                {
                    use std::os::windows::fs::MetadataExt;
                    if metadata.file_attributes() & 0x400 != 0 {
                        return Err("profile directory must not contain reparse points".into());
                    }
                }
                if metadata.file_type().is_symlink() {
                    return Err("profile directory must not contain links".into());
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(format!("profile directory unavailable: {e}")),
        }
    }
    Ok(path)
}

pub fn data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    configured_root(
        app.path().app_data_dir().map_err(|e| e.to_string())?,
        std::env::var_os("AGENT_PROFILE_DIR"),
    )
}
pub fn log_dir(app: &AppHandle) -> Result<PathBuf, String> {
    if std::env::var_os("AGENT_PROFILE_DIR").is_some() {
        data_dir(app).map(|p| p.join("logs"))
    } else {
        app.path().app_log_dir().map_err(|e| e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn default_profile_remains_unchanged() {
        let root = std::env::temp_dir().join("legacy-profile");
        assert_eq!(configured_root(root.clone(), None).unwrap(), root);
    }
    #[test]
    fn explicit_profile_selects_exact_absolute_root() {
        let root = std::env::temp_dir().join(crate::program_update::APPLICATION_ID);
        assert_eq!(
            configured_root(PathBuf::from("legacy"), Some(root.clone().into_os_string())).unwrap(),
            root
        );
    }
    #[test]
    fn arbitrary_profile_name_is_rejected_for_update_compatibility() {
        let path = std::env::temp_dir().join("arbitrary-profile");
        assert!(configured_root(PathBuf::from("legacy"), Some(path.into_os_string())).is_err());
    }
    #[test]
    fn invalid_override_never_falls_back_to_default() {
        for value in ["", "relative/profile", "../profile"] {
            assert!(configured_root(std::env::temp_dir(), Some(value.into())).is_err());
        }
        let path = std::env::temp_dir().join("one/../other");
        assert!(configured_root(std::env::temp_dir(), Some(path.into_os_string())).is_err());
    }
}
