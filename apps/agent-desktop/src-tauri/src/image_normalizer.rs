use std::{
    fs::File,
    io::{BufReader, Cursor},
    path::Path,
};

use exif::{In, Reader as ExifReader, Tag};
use image::{
    codecs::{jpeg::JpegEncoder, png::PngEncoder},
    imageops::FilterType,
    AnimationDecoder, ColorType, DynamicImage, GenericImageView, ImageEncoder, ImageReader,
};
use sha2::{Digest, Sha256};

const MAX_DECODED_PIXELS: u64 = 100_000_000;
const MAX_MODEL_PIXELS: u64 = 16_000_000;
const MAX_EDGE: u32 = 4096;
const MAX_MODEL_BYTES: usize = 10 * 1024 * 1024;

#[derive(Debug)]
pub struct NormalizedImage {
    pub bytes: Vec<u8>,
    pub media_type: &'static str,
    pub extension: &'static str,
    pub width: u32,
    pub height: u32,
    pub sha256: String,
}

pub fn normalize(path: &Path, source_media_type: &str) -> Result<NormalizedImage, String> {
    // Persisted attachment snapshots deliberately use a `.bin` suffix. Detect
    // from the actual bytes so legacy retries do not depend on a file extension.
    let dimensions_reader = ImageReader::open(path)
        .and_then(|reader| reader.with_guessed_format())
        .map_err(|_| {
            "image_normalization_failed: The image dimensions could not be read.".to_string()
        })?;
    let (width, height) = dimensions_reader.into_dimensions().map_err(|_| {
        "image_normalization_failed: The image dimensions could not be read.".to_string()
    })?;
    if u64::from(width).saturating_mul(u64::from(height)) > MAX_DECODED_PIXELS {
        return Err("image_payload_too_large: The decoded image is too large.".to_string());
    }

    let first_frame_only = source_media_type == "image/gif";
    let mut image = if first_frame_only {
        let file = File::open(path).map_err(|_| {
            "image_normalization_failed: The image could not be opened.".to_string()
        })?;
        let decoder = image::codecs::gif::GifDecoder::new(BufReader::new(file))
            .map_err(|_| "image_normalization_failed: The GIF could not be decoded.".to_string())?;
        let frame = decoder
            .into_frames()
            .next()
            .ok_or_else(|| "image_normalization_failed: The GIF has no frames.".to_string())?
            .map_err(|_| {
                "image_normalization_failed: The GIF frame could not be decoded.".to_string()
            })?;
        DynamicImage::ImageRgba8(frame.into_buffer())
    } else {
        ImageReader::open(path)
            .and_then(|reader| reader.with_guessed_format())
            .map_err(|_| "image_normalization_failed: The image could not be opened.".to_string())?
            .decode()
            .map_err(|_| {
                "image_normalization_failed: The image could not be decoded.".to_string()
            })?
    };

    image = apply_orientation(image, read_orientation(path));
    image = resize_for_model(image);
    encode_bounded(image)
}

fn read_orientation(path: &Path) -> u32 {
    let Ok(file) = File::open(path) else { return 1 };
    let mut reader = BufReader::new(file);
    let Ok(exif) = ExifReader::new().read_from_container(&mut reader) else {
        return 1;
    };
    exif.get_field(Tag::Orientation, In::PRIMARY)
        .and_then(|field| field.value.get_uint(0))
        .unwrap_or(1)
}

fn apply_orientation(image: DynamicImage, orientation: u32) -> DynamicImage {
    match orientation {
        2 => image.fliph(),
        3 => image.rotate180(),
        4 => image.flipv(),
        5 => image.rotate90().fliph(),
        6 => image.rotate90(),
        7 => image.rotate270().fliph(),
        8 => image.rotate270(),
        _ => image,
    }
}

fn resize_for_model(image: DynamicImage) -> DynamicImage {
    let (width, height) = image.dimensions();
    let edge_scale = (MAX_EDGE as f64 / f64::from(width.max(height))).min(1.0);
    let pixel_scale = (MAX_MODEL_PIXELS as f64 / (f64::from(width) * f64::from(height)))
        .sqrt()
        .min(1.0);
    let scale = edge_scale.min(pixel_scale);
    if scale >= 1.0 {
        return image;
    }
    let target_width = (f64::from(width) * scale).floor().max(1.0) as u32;
    let target_height = (f64::from(height) * scale).floor().max(1.0) as u32;
    image.resize_exact(target_width, target_height, FilterType::Lanczos3)
}

fn encode_bounded(mut image: DynamicImage) -> Result<NormalizedImage, String> {
    for _ in 0..5 {
        let has_alpha = image.color().has_alpha();
        let (bytes, media_type, extension) = if has_alpha {
            let rgba = image.to_rgba8();
            let mut output = Vec::new();
            PngEncoder::new(Cursor::new(&mut output))
                .write_image(&rgba, rgba.width(), rgba.height(), ColorType::Rgba8.into())
                .map_err(|_| {
                    "image_normalization_failed: The PNG copy could not be encoded.".to_string()
                })?;
            (output, "image/png", "png")
        } else {
            let rgb = image.to_rgb8();
            let mut output = Vec::new();
            JpegEncoder::new_with_quality(&mut output, 90)
                .encode(&rgb, rgb.width(), rgb.height(), ColorType::Rgb8.into())
                .map_err(|_| {
                    "image_normalization_failed: The JPEG copy could not be encoded.".to_string()
                })?;
            (output, "image/jpeg", "jpg")
        };
        if bytes.len() <= MAX_MODEL_BYTES {
            let (width, height) = image.dimensions();
            return Ok(NormalizedImage {
                sha256: format!("{:x}", Sha256::digest(&bytes)),
                bytes,
                media_type,
                extension,
                width,
                height,
            });
        }
        let (width, height) = image.dimensions();
        if width <= 512 && height <= 512 {
            break;
        }
        image = image.resize(
            ((width as f64) * 0.8).floor().max(1.0) as u32,
            ((height as f64) * 0.8).floor().max(1.0) as u32,
            FilterType::Lanczos3,
        );
    }
    Err("image_payload_too_large: The normalized image remains larger than 10 MiB.".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::{ImageBuffer, Rgb, Rgba};

    #[test]
    fn normalizes_opaque_images_to_metadata_free_jpeg() {
        let root = std::env::temp_dir().join(format!("agent-normalize-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let source = root.join("source.png");
        ImageBuffer::from_pixel(40, 20, Rgb([20_u8, 30, 40]))
            .save(&source)
            .unwrap();
        let normalized = normalize(&source, "image/png").unwrap();
        assert_eq!(normalized.media_type, "image/jpeg");
        assert_eq!((normalized.width, normalized.height), (40, 20));
        assert!(!normalized.bytes.is_empty());
        std::fs::remove_dir_all(root).ok();
    }

    #[test]
    fn preserves_alpha_with_png_output() {
        let root = std::env::temp_dir().join(format!("agent-normalize-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&root).unwrap();
        let source = root.join("source.png");
        ImageBuffer::from_pixel(12, 12, Rgba([20_u8, 30, 40, 100]))
            .save(&source)
            .unwrap();
        let normalized = normalize(&source, "image/png").unwrap();
        assert_eq!(normalized.media_type, "image/png");
        std::fs::remove_dir_all(root).ok();
    }
}
