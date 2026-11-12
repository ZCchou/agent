use std::path::Path;

pub const MAX_IMAGE_BYTES: u64 = 10 * 1024 * 1024;
pub const MAX_DOCUMENT_BYTES: u64 = 100 * 1024 * 1024;
pub const MAX_TEXT_BYTES: u64 = 1024 * 1024;

const IMAGE_EXTENSIONS: &[&str] = &["png", "jpg", "jpeg", "webp", "gif"];
const TEXT_EXTENSIONS: &[&str] = &[
    "txt", "md", "markdown", "json", "yaml", "yml", "toml", "js", "jsx", "ts", "tsx", "py", "rs",
    "go", "java", "c", "h", "cpp", "hpp", "cs", "css", "html", "sql", "sh", "ps1",
];

const DOCUMENT_EXTENSIONS: &[&str] = &[
    "pdf", "doc", "docx", "docm", "dot", "dotx", "dotm", "rtf", "xls", "xlsx", "xlsm", "xlt",
    "xltx", "xltm", "xlsb", "ppt", "pptx", "pptm", "pot", "potx", "potm", "pps", "ppsx", "ppsm",
    "wps", "wpt", "et", "ett", "dps", "dpt",
];

fn document_media_type(extension: &str) -> &'static str {
    match extension {
        "pdf" => "application/pdf",
        "rtf" => "application/rtf",
        "doc" | "dot" => "application/msword",
        "docx" => "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "dotx" => "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
        "docm" => "application/vnd.ms-word.document.macroEnabled.12",
        "dotm" => "application/vnd.ms-word.template.macroEnabled.12",
        "xls" | "xlt" => "application/vnd.ms-excel",
        "xlsx" => "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xltx" => "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
        "xlsm" => "application/vnd.ms-excel.sheet.macroEnabled.12",
        "xltm" => "application/vnd.ms-excel.template.macroEnabled.12",
        "xlsb" => "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
        "ppt" | "pot" | "pps" => "application/vnd.ms-powerpoint",
        "pptx" => "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "potx" => "application/vnd.openxmlformats-officedocument.presentationml.template",
        "ppsx" => "application/vnd.openxmlformats-officedocument.presentationml.slideshow",
        "pptm" => "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
        "potm" => "application/vnd.ms-powerpoint.template.macroEnabled.12",
        "ppsm" => "application/vnd.ms-powerpoint.slideshow.macroEnabled.12",
        "wps" | "wpt" => "application/kswps",
        "et" | "ett" => "application/kset",
        "dps" | "dpt" => "application/ksdps",
        _ => unreachable!(),
    }
}

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
            .chain(DOCUMENT_EXTENSIONS.iter())
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

        if DOCUMENT_EXTENSIONS.contains(&extension.as_str()) {
            let signature_matches = match extension.as_str() {
                "pdf" => detected == Some("application/pdf"),
                "rtf" => detected == Some("application/rtf"),
                "doc" | "dot" | "xls" | "xlt" | "ppt" | "pot" | "pps" => {
                    detected == Some("application/x-ole-storage")
                }
                "wps" | "wpt" | "et" | "ett" | "dps" | "dpt" => matches!(
                    detected,
                    Some("application/zip" | "application/x-ole-storage")
                ),
                _ => detected == Some("application/zip"),
            };
            if !signature_matches {
                return Err(AttachmentProbeError {
                    code: "attachment_signature_mismatch",
                    detected_media_type: detected,
                    processor_hint: Some("select_a_valid_document"),
                });
            }
            return Ok(AttachmentProbe {
                processor_id: "builtin.document",
                processor_version: "office-document-v1",
                artifact_kind: "document.source",
                kind: "text",
                media_type: document_media_type(&extension),
                max_bytes: MAX_DOCUMENT_BYTES,
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
    } else if bytes.starts_with(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") {
        Some("application/x-ole-storage")
    } else if bytes.starts_with(b"{\\rtf") {
        Some("application/rtf")
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
    fn accepts_document_containers_and_rejects_disguised_binary() {
        for (name, bytes) in [
            ("paper.pdf", b"%PDF-1.7".as_slice()),
            ("report.docx", b"PK\x03\x04".as_slice()),
            ("legacy.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1".as_slice()),
            ("notes.rtf", b"{\\rtf1 hello}".as_slice()),
            ("slides.dps", b"PK\x03\x04".as_slice()),
        ] {
            let probe = AttachmentProcessorRegistry::probe(Path::new(name), bytes).unwrap();
            assert_eq!(probe.kind, "text");
            assert_eq!(probe.artifact_kind, "document.source");
            assert_eq!(probe.processor_id, "builtin.document");
            assert_eq!(probe.processor_version, "office-document-v1");
            assert_eq!(probe.max_bytes, 100 * 1024 * 1024);
        }
        for name in ["bad.docx", "bad.xls", "bad.wps", "bad.rtf", "bad.pdf"] {
            assert_eq!(
                AttachmentProcessorRegistry::probe(Path::new(name), b"binary")
                    .unwrap_err()
                    .code,
                "attachment_signature_mismatch"
            );
        }
    }

    #[test]
    fn registers_all_document_extensions_with_their_expected_containers() {
        let ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1";
        let supported = AttachmentProcessorRegistry::supported_extensions();
        for extension in DOCUMENT_EXTENSIONS {
            assert!(supported.contains(extension));
            let bytes: &[u8] = match *extension {
                "pdf" => b"%PDF-1.7",
                "rtf" => b"{\\rtf1}",
                "doc" | "dot" | "xls" | "xlt" | "ppt" | "pot" | "pps" => ole,
                _ => b"PK\x03\x04",
            };
            let probe =
                AttachmentProcessorRegistry::probe(Path::new(&format!("file.{extension}")), bytes)
                    .unwrap();
            assert_eq!(probe.media_type, document_media_type(extension));
        }
        for extension in ["wps", "wpt", "et", "ett", "dps", "dpt"] {
            assert!(AttachmentProcessorRegistry::probe(
                Path::new(&format!("file.{extension}")),
                ole
            )
            .is_ok());
        }
        assert_eq!(
            AttachmentProcessorRegistry::probe(Path::new("a.txt"), b"text")
                .unwrap()
                .max_bytes,
            MAX_TEXT_BYTES
        );
        assert_eq!(
            AttachmentProcessorRegistry::probe(Path::new("a.png"), b"\x89PNG\r\n\x1a\n")
                .unwrap()
                .max_bytes,
            MAX_IMAGE_BYTES
        );
    }

    #[test]
    fn rejects_renamed_binary_images() {
        let error =
            AttachmentProcessorRegistry::probe(Path::new("fake.png"), b"not an image").unwrap_err();
        assert_eq!(error.code, "attachment_signature_mismatch");
    }

    #[test]
    fn reports_known_unsupported_container_types() {
        let error =
            AttachmentProcessorRegistry::probe(Path::new("paper.bin"), b"%PDF-1.7").unwrap_err();
        assert_eq!(error.code, "attachment_type_unsupported");
        assert_eq!(error.detected_media_type, Some("application/pdf"));
    }
}
