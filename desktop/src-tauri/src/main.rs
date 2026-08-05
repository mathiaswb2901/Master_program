// Release builds are GUI apps: no stray console window behind the shell.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    workbench_desktop_lib::run()
}
