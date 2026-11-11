#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
#[path = "../program_update.rs"]
mod program_update;
#[path = "../program_update_data.rs"]
mod program_update_data;
#[path = "../program_update_helper.rs"]
mod program_update_helper;
#[path = "../program_update_plan.rs"]
mod program_update_plan;
#[path = "../update_process.rs"]
mod update_process;

fn main() {
    let arguments: Vec<_> = std::env::args_os().skip(1).collect();
    if arguments.is_empty()
        || arguments.len() > 2
        || (arguments.len() == 2 && arguments[1] != "--recover")
    {
        std::process::exit(2);
    }
    let transaction = std::path::PathBuf::from(&arguments[0]);
    #[cfg(windows)]
    if let Err(error) = if arguments.len() == 2 {
        program_update_helper::recover(&transaction)
    } else {
        program_update_helper::run(&transaction)
    } {
        let result = if arguments.len() == 2 {
            "recovery-result.json"
        } else {
            "result.json"
        };
        let _ = program_update_plan::write_json(
            &transaction.join(result),
            &serde_json::json!({"status":"failed","error_code":error}),
        );
        std::process::exit(1);
    }
    #[cfg(not(windows))]
    std::process::exit(2);
}
