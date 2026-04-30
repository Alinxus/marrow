use tauri::Manager;

#[tauri::command]
fn reposition_to_bottom_right(app: tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        if let Some(monitor) = win.current_monitor().ok().flatten() {
            let size = monitor.size();
            let scale = monitor.scale_factor();
            let w = 400.0;
            let h = 58.0;
            let x = (size.width as f64 / scale) - w - 18.0;
            let y = (size.height as f64 / scale) - h - 48.0;
            let _ = win.set_position(tauri::PhysicalPosition::new(
                (x * scale) as i32,
                (y * scale) as i32,
            ));
        }
    }
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![reposition_to_bottom_right])
        .setup(|app| {
            let win = app.get_webview_window("main").unwrap();
            // Position bottom-right on startup
            if let Some(monitor) = win.current_monitor().ok().flatten() {
                let size = monitor.size();
                let scale = monitor.scale_factor();
                let w = 400.0_f64;
                let h = 58.0_f64;
                let x = (size.width as f64 / scale) - w - 18.0;
                let y = (size.height as f64 / scale) - h - 48.0;
                let _ = win.set_position(tauri::PhysicalPosition::new(
                    (x * scale) as i32,
                    (y * scale) as i32,
                ));
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Marrow");
}
