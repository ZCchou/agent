//! Windows capability backend: no-follow relative opens, pinned ancestors, handle mutations.
use super::{checked_info, error, FileAccessError, FileIdentity, HandleInfo};
use std::{
    ffi::OsStr,
    fs::{File, OpenOptions},
    mem::{offset_of, size_of},
    os::windows::{
        ffi::{OsStrExt, OsStringExt},
        fs::OpenOptionsExt,
        io::{AsRawHandle, FromRawHandle},
    },
    path::{Component, Path, PathBuf},
    ptr::{null, null_mut},
};
use windows_sys::{
    Wdk::{
        Foundation::OBJECT_ATTRIBUTES,
        Storage::FileSystem::{
            FileRenameInformation, NtCreateFile, NtSetInformationFile, FILE_CREATE,
            FILE_DIRECTORY_FILE, FILE_NON_DIRECTORY_FILE, FILE_OPEN, FILE_OPEN_REPARSE_POINT,
            FILE_RENAME_INFORMATION, FILE_SYNCHRONOUS_IO_NONALERT,
        },
    },
    Win32::{
        Foundation::{GENERIC_READ, GENERIC_WRITE, UNICODE_STRING},
        Storage::FileSystem::*,
        System::IO::IO_STATUS_BLOCK,
    },
};

pub(super) fn pin_absolute(path: &Path) -> Result<(Vec<File>, PathBuf), FileAccessError> {
    let mut components = path.components();
    let Some(Component::Prefix(prefix)) = components.next() else {
        return Err(error("path_escape"));
    };
    if !matches!(
        prefix.kind(),
        std::path::Prefix::Disk(_) | std::path::Prefix::VerbatimDisk(_)
    ) {
        return Err("file_access_root_unsupported: File tools currently support local Windows disk roots only; network and device roots remain authorized but unavailable".to_string());
    }
    if !matches!(components.next(), Some(Component::RootDir)) {
        return Err(error("path_escape"));
    }
    let mut current = PathBuf::from(prefix.as_os_str());
    current.push(r"\");
    let volume = OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)
        .open(&current)
        .map_err(map_io)?;
    checked_info(&volume, &current)?;
    let mut chain = vec![volume];
    for component in components {
        let Component::Normal(name) = component else {
            return Err(error("path_escape"));
        };
        current.push(name);
        let file = open_child(
            chain.last().expect("volume is pinned"),
            name,
            false,
            false,
            true,
        )?;
        let info = checked_info(&file, &current)?;
        if !info.is_dir {
            return Err(error("not_a_directory"));
        }
        chain.push(file);
    }
    Ok((chain, current))
}

pub(super) fn open_child(
    parent: &File,
    name: &OsStr,
    mutation: bool,
    create: bool,
    directory: bool,
) -> Result<File, FileAccessError> {
    #[cfg(test)]
    if create {
        super::tests::hook(super::tests::Stage::BeforeNativeCreate);
    }
    let mut wide: Vec<u16> = name.encode_wide().collect();
    if wide.is_empty()
        || wide.len() > 255
        || wide.contains(&0)
        || wide.contains(&(b'\\' as u16))
        || wide.contains(&(b'/' as u16))
    {
        return Err(error("invalid_path"));
    }
    let byte_len = (wide.len() * 2) as u16;
    let mut unicode = UNICODE_STRING {
        Length: byte_len,
        MaximumLength: byte_len,
        Buffer: wide.as_mut_ptr(),
    };
    let attributes = OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: parent.as_raw_handle(),
        ObjectName: &mut unicode,
        Attributes: 0x40,
        SecurityDescriptor: null_mut(),
        SecurityQualityOfService: null_mut(),
    };
    let mut result = null_mut();
    let mut status_block: IO_STATUS_BLOCK = unsafe { std::mem::zeroed() };
    let access = GENERIC_READ
        | SYNCHRONIZE
        | if mutation { DELETE } else { 0 }
        | if create && !directory {
            GENERIC_WRITE
        } else {
            0
        };
    let options = FILE_OPEN_REPARSE_POINT
        | FILE_SYNCHRONOUS_IO_NONALERT
        | if directory {
            FILE_DIRECTORY_FILE
        } else if create {
            FILE_NON_DIRECTORY_FILE
        } else {
            0
        };
    // SAFETY: structures/buffers live through the synchronous call; the parent is an
    // owned live handle. A successful returned handle is transferred once to File.
    let status = unsafe {
        NtCreateFile(
            &mut result,
            access,
            &attributes,
            &mut status_block,
            null(),
            FILE_ATTRIBUTE_NORMAL,
            FILE_SHARE_READ | if directory { FILE_SHARE_WRITE } else { 0 },
            if create { FILE_CREATE } else { FILE_OPEN },
            options,
            null(),
            0,
        )
    };
    if status < 0 {
        return Err(error(match status as u32 {
            0xc0000034 | 0xc000003a => "path_not_found",
            0xc0000035 => "file_already_exists",
            0xc0000043 => "file_access_busy",
            _ => "file_open_failed",
        }));
    }
    Ok(unsafe { File::from_raw_handle(result) })
}

pub(super) fn inspect(file: &File) -> Result<HandleInfo, FileAccessError> {
    let mut basic: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    let mut id: FILE_ID_INFO = unsafe { std::mem::zeroed() };
    // SAFETY: output buffers have the sizes required by each Windows API.
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut basic) } == 0
        || unsafe {
            GetFileInformationByHandleEx(
                file.as_raw_handle(),
                FileIdInfo,
                (&mut id as *mut FILE_ID_INFO).cast(),
                size_of::<FILE_ID_INFO>() as u32,
            )
        } == 0
    {
        return Err(error("path_inspection_failed"));
    }
    if basic.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(error("reparse_point_forbidden"));
    }
    let is_dir = basic.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0;
    if !is_dir && basic.nNumberOfLinks != 1 {
        return Err(error("hard_link_forbidden"));
    }
    let mut path = vec![0_u16; 32768];
    let length = unsafe {
        GetFinalPathNameByHandleW(
            file.as_raw_handle(),
            path.as_mut_ptr(),
            path.len() as u32,
            0,
        )
    } as usize;
    if length == 0 || length >= path.len() {
        return Err(error("path_inspection_failed"));
    }
    let path = PathBuf::from(std::ffi::OsString::from_wide(&path[..length]));
    Ok(HandleInfo {
        identity: FileIdentity {
            volume: id.VolumeSerialNumber,
            file: u128::from_le_bytes(id.FileId.Identifier),
        },
        path,
        is_dir,
        len: ((basic.nFileSizeHigh as u64) << 32) | basic.nFileSizeLow as u64,
    })
}

pub(super) fn visit_entries(
    file: &File,
    visitor: &mut dyn FnMut(String) -> bool,
) -> Result<(), FileAccessError> {
    let mut buffer = vec![0_u64; 8192];
    let mut restart = true;
    loop {
        let class = if restart {
            FileIdBothDirectoryRestartInfo
        } else {
            FileIdBothDirectoryInfo
        };
        restart = false;
        let success = unsafe {
            GetFileInformationByHandleEx(
                file.as_raw_handle(),
                class,
                buffer.as_mut_ptr().cast(),
                (buffer.len() * 8) as u32,
            )
        };
        if success == 0 {
            let failure = std::io::Error::last_os_error();
            if failure.raw_os_error() == Some(18) {
                return Ok(());
            }
            return Err(map_io(failure));
        }
        let mut offset = 0;
        loop {
            let remaining = buffer.len() * 8 - offset;
            if remaining < size_of::<FILE_ID_BOTH_DIR_INFO>() {
                return Err(error("directory_read_failed"));
            }
            // Windows fills the aligned buffer; lengths/offsets are checked before reading its tail.
            let entry = unsafe {
                &*buffer
                    .as_ptr()
                    .cast::<u8>()
                    .add(offset)
                    .cast::<FILE_ID_BOTH_DIR_INFO>()
            };
            let name_bytes = entry.FileNameLength as usize;
            if !name_bytes.is_multiple_of(2)
                || offset_of!(FILE_ID_BOTH_DIR_INFO, FileName) + name_bytes > remaining
            {
                return Err(error("directory_read_failed"));
            }
            let name =
                unsafe { std::slice::from_raw_parts(entry.FileName.as_ptr(), name_bytes / 2) };
            let name = String::from_utf16(name).map_err(|_| error("invalid_path"))?;
            if name != "." && name != ".." && !visitor(name) {
                return Ok(());
            }
            let next = entry.NextEntryOffset as usize;
            if next == 0 {
                break;
            }
            if next < size_of::<FILE_ID_BOTH_DIR_INFO>()
                || !next.is_multiple_of(8)
                || next >= remaining
            {
                return Err(error("directory_read_failed"));
            }
            offset += next;
        }
    }
}

pub(super) fn rename(file: &File, parent: &File, name: &OsStr) -> Result<(), FileAccessError> {
    let wide: Vec<u16> = name.encode_wide().collect();
    let bytes = offset_of!(FILE_RENAME_INFORMATION, FileName) + wide.len() * 2;
    // u64 storage supplies the native structure alignment and sufficient tail space.
    let mut buffer = vec![0_u64; bytes.div_ceil(8)];
    let info = buffer.as_mut_ptr().cast::<FILE_RENAME_INFORMATION>();
    // SAFETY: aligned allocation includes the variable-length UTF-16 tail. Neither
    // source nor destination parent can be redirected while their handles are held.
    let mut status_block: IO_STATUS_BLOCK = unsafe { std::mem::zeroed() };
    let status = unsafe {
        (*info).Anonymous.ReplaceIfExists = false;
        (*info).RootDirectory = parent.as_raw_handle();
        (*info).FileNameLength = (wide.len() * 2) as u32;
        std::ptr::copy_nonoverlapping(wide.as_ptr(), (*info).FileName.as_mut_ptr(), wide.len());
        NtSetInformationFile(
            file.as_raw_handle(),
            &mut status_block,
            info.cast(),
            bytes as u32,
            FileRenameInformation,
        )
    };
    if status < 0 {
        return Err(error("file_move_failed"));
    }
    Ok(())
}

pub(super) fn delete(file: &File) -> Result<(), FileAccessError> {
    let info = FILE_DISPOSITION_INFO { DeleteFile: true };
    // Windows refuses non-empty directories. No recursive removal is used here.
    if unsafe {
        SetFileInformationByHandle(
            file.as_raw_handle(),
            FileDispositionInfo,
            (&info as *const FILE_DISPOSITION_INFO).cast(),
            size_of::<FILE_DISPOSITION_INFO>() as u32,
        )
    } == 0
    {
        return Err(map_io(std::io::Error::last_os_error()));
    }
    Ok(())
}
fn map_io(error_value: std::io::Error) -> FileAccessError {
    error(match error_value.raw_os_error() {
        Some(2 | 3) => "path_not_found",
        Some(80 | 183) => "file_already_exists",
        Some(145) => "directory_not_empty",
        Some(32 | 33) => "file_access_busy",
        _ => "file_access_failed",
    })
}
