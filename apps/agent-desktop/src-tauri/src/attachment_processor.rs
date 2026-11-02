use std::path::Path;

pub const MAX_IMAGE_BYTES: u64 = 10 * 1024 * 1024;
pub const MAX_TEXT_BYTES: u64 = 1024 * 1024;

const IMAGE_EXTENSIONS: &[&str] = &["png", "jpg", "jpeg", "webp", "gif"];
const TEXT_EXTENSIONS: &[&str] = &[
    "txt", "md", "markdown", "json", "yaml", "yml", "toml", "js", "jsx", "ts", "tsx", "py", "rs",
    "go", "java", "c", "h", "cpp", "hpp", "cs", "css", "html", "sql", "sh", "ps1",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttachmentProbe {
    pub processor_id: &'static str,
    pub processor_version: &'static str,
    pub artifact_kind: &'static str,
    pub kind: &'static str,
    pub media_type: &'static str,
    pub max_bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttachmentProbeError {
    pub code: &'static str,
    pub detected_media_type: Option<&'static str>,
    pub processor_hint: Option<&'static str>,
}

impl AttachmentProbeError {
    pub fn unsupported(detected_media_type: Option<&'static str>) -> Self {
        Self {
            code: "attachment_type_unsupported",
            detected_media_type,
            processor_hint: Some("convert_to_supported_format"),
        }
    }
}

/// Registry for deterministic, built-in attachment processors.
///
/// It deliberately performs no writes and owns the only supported-extension
/// list used by both the native picker and import validation.
pub struct AttachmentProcessorRegistry;

impl AttachmentProcessorRegistry {
    pub fn supported_extensions() -> Vec<&'static str> {
        IMAGE_EXTENSIONS
            .iter()
            .chain(TEXT_EXTENSIONS.iter())
            .copied()
            .collect()
    }

    pub fn probe(path: &Path, bytes: &[u8]) -> Result<AttachmentProbe, AttachmentProbeError> {
        let extension = path
            .extension()
            .and_then(|value| value.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        let detected = detect_media_type(bytes);

        if IMAGE_EXTENSIONS.contains(&extension.as_str()) {
            let expected = match extension.as_str() {
                "png" => "image/png",
                "jpg" | "jpeg" => "image/jpeg",
                "webp" => "image/webp",
                "gif" => "image/gif",
                _ => unreachable!(),
            };
            if detected != Some(expected) {
                return Err(AttachmentProbeError {
                    code: "attachment_signature_mismatch",
                    detected_media_type: detected,
                    processor_hint: Some("select_a_valid_image"),
                });
            }
            return Ok(AttachmentProbe {
                processor_id: "builtin.image",
                processor_version: "image-normalization-v1",
                artifact_kind: "image.normalized",
                kind: "image",
                media_type: expected,
                max_bytes: MAX_IMAGE_BYTES,
            });
        }

        if TEXT_EXTENSIONS.contains(&extension.as_str()) {
            if std::str::from_utf8(bytes).is_err() {
                return Err(AttachmentProbeError {
                    code: "invalid_attachment_encoding",
                    detected_media_type: detected,
                    processor_hint: Some("convert_to_utf8"),
                });
            }
            let media_type = match extension.as_str() {
                "md" | "markdown" => "text/markdown",
                "json" => "application/json",
                "yaml" | "yml" => "application/yaml",
                "toml" => "application/toml",
                _ => "text/plain",
            };
            return Ok(AttachmentProbe {
                processor_id: "builtin.utf8_text",
                processor_version: "utf8-text-v1",
                artifact_kind: "text.utf8",
                kind: "text",
                media_type,
                max_bytes: MAX_TEXT_BYTES,
            });
        }

        Err(AttachmentProbeError::unsupported(detected))
    }
}

fn detect_media_type(bytes: &[u8]) -> Option<&'static str> {
    if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("image/png")
    } else if bytes.starts_with(&[0xff, 0xd8, 0xff]) {
        Some("image/jpeg")
    } else if bytes.starts_with(b"GIF87a") || bytes.starts_with(b"GIF89a") {
        Some("image/gif")
    } else if bytes.len() >= 12 && &bytes[..4] == b"RIFF" && &bytes[8..12] == b"WEBP" {
        Some("image/webp")
    } else if bytes.starts_with(b"%PDF-") {
        Some("application/pdf")
    } else if bytes.starts_with(b"PK\x03\x04") {
        Some("application/zip")
    } else if bytes.starts_with(b"ID3") {
        Some("audio/mpeg")
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_renamed_binary_images() {
        let error =
            AttachmentProcessorRegistry::probe(Path::new("fake.png"), b"not an image").unwrap_err();
        assert_eq!(error.code, "attachment_signature_mismatch");
    }

    #[test]
    fn reports_known_unsupported_container_types() {
        let error =
            AttachmentProcessorRegistry::probe(Path::new("paper.pdf"), b"%PDF-1.7").unwrap_err();
        assert_eq!(error.code, "attachment_type_unsupported");
        assert_eq!(error.detected_media_type, Some("application/pdf"));
    }
}
