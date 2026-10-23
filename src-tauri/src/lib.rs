use serde::Serialize;
use std::{
    fs,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};
use tauri::{AppHandle, Manager, RunEvent};
use tauri_plugin_shell::{process::CommandChild, ShellExt};
use uuid::Uuid;

const SIDECAR_NAME: &str = "sortly-api";
const SHUTDOWN_TOKEN_ENV: &str = "SORTLY_SHUTDOWN_TOKEN";
const MAX_PORT_ATTEMPTS: u8 = 6;
const STARTUP_TIMEOUT: Duration = Duration::from_secs(75);

#[derive(Clone, Serialize)]
struct ApiStatus {
    state: String,
    base_url: Option<String>,
    detail: Option<String>,
}

struct SidecarRuntime {
    status: ApiStatus,
    child: Option<CommandChild>,
    shutdown_token: Option<String>,
    port: Option<u16>,
    stopping: bool,
}

struct SidecarState(Mutex<SidecarRuntime>);

impl Default for SidecarState {
    fn default() -> Self {
        Self(Mutex::new(SidecarRuntime {
            status: ApiStatus {
                state: "starting".into(),
                base_url: None,
                detail: None,
            },
            child: None,
            shutdown_token: None,
            port: None,
            stopping: false,
        }))
    }
}

#[tauri::command]
fn get_api_status(state: tauri::State<'_, SidecarState>) -> ApiStatus {
    state.0.lock().unwrap().status.clone()
}

fn set_status(handle: &AppHandle, status: ApiStatus) {
    if let Some(state) = handle.try_state::<SidecarState>() {
        state.0.lock().unwrap().status = status;
    }
}

fn reserve_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .map_err(|error| format!("Could not reserve a local API port: {error}"))?;
    listener
        .local_addr()
        .map(|address| address.port())
        .map_err(|error| format!("Could not inspect the reserved API port: {error}"))
}

fn local_http(
    port: u16,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
) -> std::io::Result<String> {
    let address = format!("127.0.0.1:{port}");
    let mut stream = TcpStream::connect_timeout(
        &address.parse().expect("loopback address is valid"),
        Duration::from_millis(750),
    )?;
    stream.set_read_timeout(Some(Duration::from_secs(2)))?;
    stream.set_write_timeout(Some(Duration::from_secs(2)))?;

    let mut request =
        format!("{method} {path} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n");
    for (name, value) in headers {
        request.push_str(&format!("{name}: {value}\r\n"));
    }
    request.push_str("Content-Length: 0\r\n\r\n");
    stream.write_all(request.as_bytes())?;
    stream.flush()?;

    let mut response = String::new();
    stream.read_to_string(&mut response)?;
    Ok(response)
}

fn sidecar_healthy(port: u16) -> bool {
    local_http(port, "GET", "/api/health", &[])
        .map(|response| {
            response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200")
        })
        .unwrap_or(false)
}

fn sidecar_environment(app: &AppHandle) -> Result<(PathBuf, Option<PathBuf>), String> {
    let config_dir = app
        .path()
        .app_config_dir()
        .map_err(|error| format!("Could not resolve the app configuration directory: {error}"))?;
    let data_dir = std::env::var_os("SORTLY_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or(
            app.path()
                .app_local_data_dir()
                .map_err(|error| format!("Could not resolve the app data directory: {error}"))?,
        );
    fs::create_dir_all(&config_dir)
        .map_err(|error| format!("Could not create the app configuration directory: {error}"))?;
    fs::create_dir_all(&data_dir)
        .map_err(|error| format!("Could not create the app data directory: {error}"))?;

    let configured = std::env::var_os("SORTLY_ENV_FILE")
        .map(PathBuf::from)
        .filter(|path| path.is_file());
    let app_env = config_dir.join("sortly.env");
    let development_env = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(|root| root.join(".env"))
        .filter(|path| cfg!(debug_assertions) && path.is_file());
    let env_file = configured
        .or_else(|| app_env.is_file().then_some(app_env))
        .or(development_env);
    Ok((data_dir, env_file))
}

fn start_sidecar(handle: AppHandle) {
    let (data_dir, env_file) = match sidecar_environment(&handle) {
        Ok(value) => value,
        Err(error) => {
            set_status(
                &handle,
                ApiStatus {
                    state: "error".into(),
                    base_url: None,
                    detail: Some(error),
                },
            );
            return;
        }
    };

    for attempt in 1..=MAX_PORT_ATTEMPTS {
        if handle
            .try_state::<SidecarState>()
            .map(|state| state.0.lock().unwrap().stopping)
            .unwrap_or(true)
        {
            return;
        }

        let port = match reserve_port() {
            Ok(port) => port,
            Err(error) => {
                set_status(
                    &handle,
                    ApiStatus {
                        state: "error".into(),
                        base_url: None,
                        detail: Some(error),
                    },
                );
                return;
            }
        };
        let token = Uuid::new_v4().simple().to_string();
        let port_text = port.to_string();
        let cache_db = data_dir.join("cache.db");
        let mut command = match handle.shell().sidecar(SIDECAR_NAME) {
            Ok(command) => command,
            Err(error) => {
                set_status(
                    &handle,
                    ApiStatus {
                        state: "error".into(),
                        base_url: None,
                        detail: Some(format!("Could not prepare the local service: {error}")),
                    },
                );
                return;
            }
        };
        command = command
            .set_process_group(true)
            .args(["--port", port_text.as_str(), "--log-level", "warning"])
            .env(SHUTDOWN_TOKEN_ENV, token.clone())
            .env("ORGANIZER_HOME", data_dir.to_string_lossy().to_string())
            .env("ORGANIZER_CACHE_DB", cache_db.to_string_lossy().to_string());
        if let Some(path) = &env_file {
            command = command.args(["--env-file", path.to_string_lossy().as_ref()]);
        }

        let (mut events, child) = match command.spawn() {
            Ok(value) => value,
            Err(error) => {
                if attempt == MAX_PORT_ATTEMPTS {
                    set_status(
                        &handle,
                        ApiStatus {
                            state: "error".into(),
                            base_url: None,
                            detail: Some(format!("Could not launch the local service: {error}")),
                        },
                    );
                }
                continue;
            }
        };
        tauri::async_runtime::spawn(async move { while events.recv().await.is_some() {} });

        set_status(
            &handle,
            ApiStatus {
                state: "starting".into(),
                base_url: None,
                detail: Some(format!(
                    "Starting local service (attempt {attempt}/{MAX_PORT_ATTEMPTS})"
                )),
            },
        );

        let deadline = Instant::now() + STARTUP_TIMEOUT;
        while Instant::now() < deadline {
            if sidecar_healthy(port) {
                let base_url = format!("http://127.0.0.1:{port}");
                if let Some(state) = handle.try_state::<SidecarState>() {
                    let mut runtime = state.0.lock().unwrap();
                    if runtime.stopping {
                        drop(runtime);
                        let _ = child.kill();
                        return;
                    }
                    runtime.child = Some(child);
                    runtime.shutdown_token = Some(token);
                    runtime.port = Some(port);
                    runtime.status = ApiStatus {
                        state: "ready".into(),
                        base_url: Some(base_url.clone()),
                        detail: None,
                    };
                }
                return;
            }
            thread::sleep(Duration::from_millis(250));
        }

        let _ = child.kill();
    }

    set_status(
        &handle,
        ApiStatus {
            state: "error".into(),
            base_url: None,
            detail: Some("The local service could not bind to a free loopback port.".into()),
        },
    );
}

fn shutdown_sidecar(handle: &AppHandle) {
    let (port, token, child) = {
        let Some(state) = handle.try_state::<SidecarState>() else {
            return;
        };
        let mut runtime = state.0.lock().unwrap();
        if runtime.stopping {
            return;
        }
        runtime.stopping = true;
        (
            runtime.port.take(),
            runtime.shutdown_token.take(),
            runtime.child.take(),
        )
    };

    if let (Some(port), Some(token)) = (port, token) {
        let _ = local_http(
            port,
            "POST",
            "/api/shutdown",
            &[("X-Sortly-Shutdown-Token", token.as_str())],
        );

        let deadline = Instant::now() + Duration::from_secs(8);
        while Instant::now() < deadline {
            if TcpStream::connect_timeout(
                &format!("127.0.0.1:{port}")
                    .parse()
                    .expect("loopback address is valid"),
                Duration::from_millis(200),
            )
            .is_err()
            {
                return;
            }
            thread::sleep(Duration::from_millis(150));
        }
    }

    if let Some(child) = child {
        let _ = child.kill();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(SidecarState::default())
        .invoke_handler(tauri::generate_handler![get_api_status])
        .setup(|app| {
            let handle = app.handle().clone();
            thread::spawn(move || start_sidecar(handle));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Sortly");

    app.run(|handle, event| {
        if matches!(event, RunEvent::Exit) {
            shutdown_sidecar(handle);
        }
    });
}
