#[path = "../installer_native.rs"]
mod installer_native;

fn main() {
    if let Err(error) = installer_native::main_entry() {
        eprintln!("OFFLINE_INSTALL_ERROR {error}");
        std::process::exit(1);
    }
}
