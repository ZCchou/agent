#[path = "../installer_native.rs"]
mod installer_native;
#[path = "../installer_process.rs"]
mod installer_process;

fn main() {
    if let Err(error) = installer_native::main_entry() {
        eprintln!("OFFLINE_INSTALL_ERROR {error}");
        std::process::exit(1);
    }
}
