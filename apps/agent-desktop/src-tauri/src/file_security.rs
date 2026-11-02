use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OperationRisk {
    Routine,
    Medium,
    High,
    Forbidden,
}

pub fn classify_mutations(mutations: &[Value]) -> OperationRisk {
    let mut risk = if mutations.len() > 20 {
        OperationRisk::High
    } else if mutations.len() > 3 {
        OperationRisk::Medium
    } else {
        OperationRisk::Routine
    };
    let changed_lines = mutations
        .iter()
        .filter_map(|item| {
            let text_field = match item.get("kind").and_then(Value::as_str) {
                Some("patch_text") => "new_text",
                Some("create_text" | "overwrite_text") => "content",
                _ => return None,
            };
            item.get(text_field).and_then(Value::as_str)
        })
        .map(str::lines)
        .map(Iterator::count)
        .sum::<usize>();
    if changed_lines > 1_000 {
        risk = OperationRisk::High;
    } else if changed_lines > 200 && risk == OperationRisk::Routine {
        risk = OperationRisk::Medium;
    }
    for mutation in mutations {
        let kind = mutation
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let path = mutation
            .get("path")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let source = mutation
            .get("source_path")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if is_forbidden_path(path)
            || (kind == "delete" && matches!(path, "." | "./"))
            || (!source.is_empty() && is_forbidden_path(source))
        {
            return OperationRisk::Forbidden;
        }
        if kind == "delete" || is_sensitive_path(path) || is_likely_binary_path(path) {
            risk = OperationRisk::High;
        } else if matches!(kind, "copy" | "move" | "overwrite_text" | "patch_text")
            && risk == OperationRisk::Routine
        {
            risk = OperationRisk::Medium;
        }
    }
    risk
}

fn is_likely_binary_path(path: &str) -> bool {
    let normalized = path.replace('\\', "/").to_ascii_lowercase();
    matches!(
        normalized.rsplit('.').next().unwrap_or_default(),
        "exe"
            | "dll"
            | "bin"
            | "zip"
            | "7z"
            | "rar"
            | "pdf"
            | "png"
            | "jpg"
            | "jpeg"
            | "gif"
            | "webp"
            | "mp3"
            | "wav"
            | "mp4"
            | "mov"
            | "sqlite"
            | "db"
    )
}

pub fn validate_relative(path: &str) -> Result<PathBuf, String> {
    crate::file_access::FileAccessPolicy
        .validate_relative(path)
        .map(|path| path.as_path().to_path_buf())
}
fn is_forbidden_path(path: &str) -> bool {
    validate_relative(path).is_err()
}
#[cfg(test)]
fn resolve_existing(root: &std::path::Path, relative: &str) -> Result<PathBuf, String> {
    let scope = crate::file_access::AuthorizedRoot::for_host_path(root, std::path::Path::new(""));
    let policy = crate::file_access::FileAccessPolicy;
    Ok(policy
        .open_read(&scope, &policy.validate_relative(relative)?)?
        .path()
        .to_path_buf())
}

fn is_sensitive_path(path: &str) -> bool {
    let normalized = path.replace('\\', "/").to_ascii_lowercase();
    let file = normalized.rsplit('/').next().unwrap_or_default();
    file.ends_with(".lock")
        || file.ends_with(".bat")
        || file.ends_with(".ps1")
        || file.ends_with(".sh")
        || file == "package.json"
        || file == "cargo.toml"
        || file == "pyproject.toml"
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn file_security_rejects_sensitive_components_and_windows_aliases() {
        for path in [
            "nested/.git/config",
            "nested/credentials/data.txt",
            "secrets/data.txt",
            "nested/.ENV.local",
            "nested/.git./config",
            "nested/.env ",
            "normal.txt:private",
            "NUL",
            "aux.txt",
            r"\\?\C:\normal.txt",
            r"\\server\share\normal.txt",
            "normal/../note.txt",
            "nested/CONIN$",
            "nested/COM1.txt",
            "nested/short~1/file",
        ] {
            assert!(
                validate_relative(path).is_err(),
                "allowed alias or sensitive component: {path}"
            );
        }
        assert!(validate_relative(r"nested\normal.txt").is_ok());
    }

    #[cfg(windows)]
    #[test]
    fn file_security_rejects_original_junction_chain_inside_root() {
        let root = std::env::temp_dir().join(format!("agent-policy-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(root.join("actual")).unwrap();
        fs::write(root.join("actual/note.txt"), "synthetic text").unwrap();
        let status = std::process::Command::new("cmd")
            .args(["/C", "mklink", "/J"])
            .arg(root.join("alias"))
            .arg(root.join("actual"))
            .output()
            .unwrap();
        assert!(status.status.success());
        let result = resolve_existing(&root.canonicalize().unwrap(), "alias/note.txt");
        fs::remove_dir(root.join("alias")).unwrap();
        fs::remove_dir_all(&root).unwrap();
        assert!(
            result.is_err(),
            "original junction was erased by canonicalize"
        );
    }

    #[test]
    fn risk_matrix_is_deterministic() {
        assert_eq!(
            classify_mutations(&[serde_json::json!({
                "kind": "create_text", "path": "notes.txt", "content": "hello"
            })]),
            OperationRisk::Routine
        );
        assert_eq!(
            classify_mutations(&[serde_json::json!({
                "kind": "delete", "path": "notes.txt"
            })]),
            OperationRisk::High
        );
        assert_eq!(
            classify_mutations(&[serde_json::json!({
                "kind": "overwrite_text", "path": ".git/config", "content": "bad"
            })]),
            OperationRisk::Forbidden
        );
    }

    #[test]
    fn rejects_absolute_parent_and_project_root_paths() {
        assert!(validate_relative("../secret").is_err());
        assert!(validate_relative(r"C:\secret").is_err());
        assert!(validate_relative(".").is_ok());
        assert!(validate_relative("src/main.rs").is_ok());
        assert!(validate_relative("./").is_ok());
        assert!(validate_relative("src//main.rs").is_ok());
    }
}
