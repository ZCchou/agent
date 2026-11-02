//! Fail closed until an equivalent anchored implementation is supplied for this OS.
use super::{error, FileAccessError, HandleInfo};
use std::{
    ffi::OsStr,
    fs::File,
    path::{Path, PathBuf},
};
fn unavailable<T>() -> Result<T, FileAccessError> {
    Err(error("file_access_platform_unsupported"))
}
pub(super) fn pin_absolute(_: &Path) -> Result<(Vec<File>, PathBuf), FileAccessError> {
    unavailable()
}
pub(super) fn open_child(
    _: &File,
    _: &OsStr,
    _: bool,
    _: bool,
    _: bool,
) -> Result<File, FileAccessError> {
    unavailable()
}
pub(super) fn inspect(_: &File) -> Result<HandleInfo, FileAccessError> {
    unavailable()
}
pub(super) fn rename(_: &File, _: &File, _: &OsStr) -> Result<(), FileAccessError> {
    unavailable()
}
pub(super) fn delete(_: &File) -> Result<(), FileAccessError> {
    unavailable()
}

pub(super) fn visit_entries(
    _: &File,
    _: &mut dyn FnMut(String) -> bool,
) -> Result<(), FileAccessError> {
    unavailable()
}
