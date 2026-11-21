//! The program has no console of its own on Windows. Started from a terminal it borrows that
//! terminal's console, so help, errors and logs still print there; otherwise it stays silent
//! (the Logs page keeps the log).

/// Points the standard output and error streams at the parent's console, unless they are
/// already redirected somewhere.
#[cfg(windows)]
#[allow(unsafe_code)]
pub fn attach_to_parent() {
    use std::fs::OpenOptions;
    use std::os::windows::io::IntoRawHandle;

    use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
    use windows_sys::Win32::System::Console::{
        ATTACH_PARENT_PROCESS, AttachConsole, GetStdHandle, STD_ERROR_HANDLE, STD_OUTPUT_HANDLE,
        SetStdHandle,
    };

    // SAFETY: plain calls with handle constants and no pointers; a handle given to
    // SetStdHandle stays open for the life of the process.
    unsafe {
        let unset = |which| {
            let handle = GetStdHandle(which);
            handle.is_null() || handle == INVALID_HANDLE_VALUE
        };
        let streams: Vec<_> = [STD_OUTPUT_HANDLE, STD_ERROR_HANDLE]
            .into_iter()
            .filter(|&which| unset(which))
            .collect();
        if streams.is_empty() || AttachConsole(ATTACH_PARENT_PROCESS) == 0 {
            return;
        }
        for which in streams {
            if let Ok(console) = OpenOptions::new().write(true).open("CONOUT$") {
                SetStdHandle(which, console.into_raw_handle());
            }
        }
    }
}

#[cfg(not(windows))]
pub fn attach_to_parent() {}
