//! Interactive `lethe init` wizard.
//!
//! 1. Detects any LLM credentials already in the environment (and the
//!    existing `~/.lethe/config/.env` if one exists).
//! 2. Walks the user through provider / model / key / Telegram choices.
//! 3. Writes `~/.lethe/config/.env` and seeds the workspace + default memory
//!    blocks.
//! 4. Runs a smoke test (model ping + embedding probe).
//! 5. Tells them what to run next.
//!
//! TTY-aware: refuses to prompt over a non-terminal stdin (use the env
//! variables directly in scripted contexts).

use std::io::{self, BufRead, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, anyhow, bail};
use lethe::config::Settings;
use lethe::llm::models::{model_available_for_provider, model_catalog, openai_oauth_token_file};
use lethe::llm::{
    LlmMessage, LlmRouter, LlmRouterConfig, OpenAIOAuthTokens, extract_openai_account_id,
    read_openai_oauth_tokens, write_openai_oauth_tokens,
};
use lethe::memory::{BlockManager, MemoryStore};
use serde_json::Value;

const OPENAI_CLIENT_ID: &str = "app_EMoamEEZ73f0CkXaXp7hrann";
const OPENAI_DEVICE_USERCODE_URL: &str = "https://auth.openai.com/api/accounts/deviceauth/usercode";
const OPENAI_DEVICE_TOKEN_URL: &str = "https://auth.openai.com/api/accounts/deviceauth/token";
const OPENAI_TOKEN_URL: &str = "https://auth.openai.com/oauth/token";
const OPENAI_DEVICE_REDIRECT_URI: &str = "https://auth.openai.com/deviceauth/callback";
const OPENAI_LOGIN_USER_AGENT: &str = "lethe-oauth-login";
const OPENAI_DEVICE_AUTH_TIMEOUT_SECONDS: u64 = 900;
const OPENAI_DEVICE_POLL_SAFETY_MARGIN_SECONDS: u64 = 3;

/// Top-level entry point.
pub async fn run() -> Result<()> {
    if !io::stdin().is_terminal() {
        bail!(
            "`lethe init` needs an interactive terminal. \
             Set env vars manually (see .env.example) or pipe a config into a file."
        );
    }

    print_header();

    let settings = Settings::from_env();
    let lethe_home = settings.paths.lethe_home.clone();
    let env_path = lethe_home.join("config").join(".env");
    let existing_env = read_existing_env(&env_path);

    println!("Lethe will store config + state under:");
    println!("  {}\n", lethe_home.display());

    let detected = detect_keys(&existing_env);
    if !detected.is_empty() {
        println!(
            "Found existing LLM credentials for: {}\n",
            detected.join(", ")
        );
    }

    // -- Provider -----------------------------------------------------------
    let provider = prompt_provider(&detected)?;
    info(&format!("Using {}", provider.label()));

    // -- Models -------------------------------------------------------------
    let (main_model, mut aux_model) = prompt_models(provider)?;
    info(&format!("Main: {main_model}"));
    info(&format!("Aux:  {aux_model}"));

    // -- Auth ---------------------------------------------------------------
    let (api_key, openai_oauth_tokens) = match provider {
        Provider::OpenAI => {
            let auth = prompt_openai_auth(&existing_env, &main_model).await?;
            if matches!(&auth, OpenAIAuthChoice::Subscription { .. }) {
                let adjusted_aux = openai_subscription_aux_model(&main_model, &aux_model);
                if adjusted_aux != aux_model {
                    info(&format!(
                        "OpenAI subscription selected; using {adjusted_aux} for aux calls because `{}` isn't available under OpenAI.",
                        aux_model
                    ));
                    aux_model = adjusted_aux;
                }
            }
            (
                auth.api_key().map(str::to_string),
                auth.token_file().map(Path::to_path_buf),
            )
        }
        _ => (Some(prompt_api_key(provider, &existing_env)?), None),
    };

    // -- Optional Telegram --------------------------------------------------
    let telegram = prompt_telegram(&existing_env)?;

    // -- Optional human-block intro ----------------------------------------
    let human_intro = prompt_human_intro()?;

    // -- Persist ------------------------------------------------------------
    write_env_file(
        &env_path,
        provider,
        &main_model,
        &aux_model,
        api_key.as_deref(),
        openai_oauth_tokens.as_deref(),
        telegram.as_ref(),
    )?;
    info(&format!("Wrote {}", env_path.display()));

    seed_workspace(&settings)?;
    if let Some(text) = human_intro {
        seed_human_block(&settings, &text)?;
    }
    info("Seeded workspace + memory blocks.");

    // -- Smoke test ---------------------------------------------------------
    println!("\nRunning smoke test...");
    smoke_test(
        provider,
        &main_model,
        &aux_model,
        api_key.as_deref(),
        openai_oauth_tokens.as_deref(),
    )
    .await?;

    println!();
    success("Setup complete.");
    println!();
    println!("Next steps:");
    println!("  lethe chat -m \"hello\"     # one-off chat");
    println!("  lethe                       # default mode (cli)");
    if telegram.is_some() {
        println!("  lethe telegram run          # start Telegram bot");
    }
    println!("  lethe check                 # health check any time");
    Ok(())
}

// =============================================================================
// Provider selection
// =============================================================================

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Provider {
    OpenRouter,
    Anthropic,
    OpenAI,
}

impl Provider {
    fn label(self) -> &'static str {
        match self {
            Provider::OpenRouter => "OpenRouter",
            Provider::Anthropic => "Anthropic",
            Provider::OpenAI => "OpenAI",
        }
    }
    fn id(self) -> &'static str {
        match self {
            Provider::OpenRouter => "openrouter",
            Provider::Anthropic => "anthropic",
            Provider::OpenAI => "openai",
        }
    }
    fn key_env(self) -> &'static str {
        match self {
            Provider::OpenRouter => "OPENROUTER_API_KEY",
            Provider::Anthropic => "ANTHROPIC_API_KEY",
            Provider::OpenAI => "OPENAI_API_KEY",
        }
    }
    fn key_url(self) -> &'static str {
        match self {
            Provider::OpenRouter => "https://openrouter.ai/keys",
            Provider::Anthropic => "https://console.anthropic.com/settings/keys",
            Provider::OpenAI => "https://platform.openai.com/api-keys",
        }
    }
}

#[derive(Clone, Debug)]
enum OpenAIAuthChoice {
    ApiKey(String),
    Subscription { token_file: PathBuf },
}

impl OpenAIAuthChoice {
    fn api_key(&self) -> Option<&str> {
        match self {
            Self::ApiKey(key) => Some(key.as_str()),
            Self::Subscription { .. } => None,
        }
    }

    fn token_file(&self) -> Option<&Path> {
        match self {
            Self::ApiKey(_) => None,
            Self::Subscription { token_file } => Some(token_file.as_path()),
        }
    }
}

fn detect_keys(existing_env: &EnvMap) -> Vec<&'static str> {
    let token_file = openai_oauth_token_file();
    let mut out = Vec::new();

    for provider in [Provider::OpenRouter, Provider::Anthropic, Provider::OpenAI] {
        let present = match provider {
            Provider::OpenAI => {
                let openai_api_key = env_present(provider.key_env(), existing_env);
                let openai_subscription = env_present("OPENAI_AUTH_TOKEN", existing_env)
                    || read_openai_oauth_tokens(&token_file).is_some();
                openai_api_key || openai_subscription
            }
            _ => env_present(provider.key_env(), existing_env),
        };

        if present {
            out.push(provider.key_env());
        }
    }

    if env_present("OPENAI_AUTH_TOKEN", existing_env)
        || read_openai_oauth_tokens(&token_file).is_some()
    {
        out.push("OPENAI_AUTH_TOKEN");
    }

    out.sort_unstable();
    out.dedup();
    out
}

fn prompt_provider(detected: &[&'static str]) -> Result<Provider> {
    println!("Select your LLM provider:\n");
    let entries = [
        (
            Provider::OpenRouter,
            "OpenRouter (recommended — single key, every major model)",
        ),
        (
            Provider::Anthropic,
            "Anthropic (API key or Claude subscription token)",
        ),
        (
            Provider::OpenAI,
            "OpenAI (API key or ChatGPT subscription login)",
        ),
    ];
    for (idx, (provider, desc)) in entries.iter().enumerate() {
        let badge = provider_badge(*provider, detected);
        println!("  {}) {desc}{badge}", idx + 1);
    }

    let default = entries
        .iter()
        .position(|(provider, _)| provider_detected(*provider, detected))
        .map(|i| i + 1)
        .unwrap_or(1);

    let choice = prompt_line(&format!("\nChoose [1-3, default={default}]: "))?;
    let choice = choice.trim();
    let n = if choice.is_empty() {
        default
    } else {
        choice
            .parse::<usize>()
            .ok()
            .filter(|n| (1..=entries.len()).contains(n))
            .unwrap_or(default)
    };
    Ok(entries[n - 1].0)
}

fn provider_detected(provider: Provider, detected: &[&'static str]) -> bool {
    match provider {
        Provider::OpenAI => {
            detected.contains(&"OPENAI_API_KEY") || detected.contains(&"OPENAI_AUTH_TOKEN")
        }
        _ => detected.contains(&provider.key_env()),
    }
}

fn provider_badge(provider: Provider, detected: &[&'static str]) -> &'static str {
    match provider {
        Provider::OpenAI if detected.contains(&"OPENAI_AUTH_TOKEN") => " [subscription found]",
        Provider::OpenAI if detected.contains(&"OPENAI_API_KEY") => " [key found]",
        _ if detected.contains(&provider.key_env()) => " [key found]",
        _ => "",
    }
}

async fn prompt_openai_auth(existing_env: &EnvMap, main_model: &str) -> Result<OpenAIAuthChoice> {
    let token_file = openai_oauth_token_file();
    let existing_token = read_openai_oauth_tokens(&token_file);
    let existing_env_token = env_value("OPENAI_AUTH_TOKEN", existing_env);

    println!("\nOpenAI auth choice:");
    println!("  1) OpenAI API key");
    println!("  2) ChatGPT subscription login");
    if existing_token.is_some() {
        println!(
            "  Found existing subscription token file: {}",
            token_file.display()
        );
    } else if existing_env_token.is_some() {
        println!("  Found existing OPENAI_AUTH_TOKEN in env.");
    }

    let default = if existing_token.is_some() || existing_env_token.is_some() {
        2
    } else {
        1
    };
    let answer = prompt_line(&format!("  Choose [1-2, default={default}]: "))?;
    let choice = answer
        .trim()
        .parse::<usize>()
        .ok()
        .filter(|n| (1..=2).contains(n))
        .unwrap_or(default);

    match choice {
        1 => Ok(OpenAIAuthChoice::ApiKey(prompt_api_key(
            Provider::OpenAI,
            existing_env,
        )?)),
        _ => {
            validate_openai_subscription_model(main_model)?;
            if existing_token.is_some() {
                let reuse = prompt_line("  Reuse existing subscription token file? [Y/n]: ")?;
                if !reuse.trim().to_ascii_lowercase().starts_with('n') {
                    return Ok(OpenAIAuthChoice::Subscription { token_file });
                }
            }

            run_openai_oauth_login(&token_file).await?;
            Ok(OpenAIAuthChoice::Subscription { token_file })
        }
    }
}

fn validate_openai_subscription_model(main_model: &str) -> Result<()> {
    if !model_available_for_provider("openai", main_model) {
        bail!(
            "OpenAI subscription login requires a model available under OpenAI. `{main_model}` is not in the OpenAI catalog — choose an OpenAI API key or a different model."
        );
    }
    Ok(())
}

fn openai_subscription_aux_model(main_model: &str, aux_model: &str) -> String {
    if model_available_for_provider("openai", aux_model) {
        aux_model.to_string()
    } else {
        main_model.to_string()
    }
}

async fn run_openai_oauth_login(token_file: &Path) -> Result<()> {
    println!("\nOpenAI OAuth Login (ChatGPT Plus/Pro Codex)\n");
    println!("This uses device flow, suitable for local and headless environments.");
    println!("1) Open the verification URL");
    println!("2) Enter the code shown below");
    println!("3) Return to this terminal and wait for completion\n");

    let device = start_openai_device_flow().await?;
    let device_auth_id = device
        .get("device_auth_id")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let user_code = device
        .get("user_code")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let interval = device.get("interval").and_then(Value::as_u64).unwrap_or(5);
    let verify_url = device
        .get("verification_uri")
        .and_then(Value::as_str)
        .or_else(|| {
            device
                .get("verification_uri_complete")
                .and_then(Value::as_str)
        })
        .unwrap_or("https://auth.openai.com/codex/device");

    if device_auth_id.is_empty() || user_code.is_empty() {
        bail!("Invalid device authorization response: {device:?}");
    }

    println!("Verification URL: {verify_url}");
    println!("User code: {user_code}\n");
    try_open_browser(verify_url);
    println!("Waiting for authorization (Ctrl+C to cancel)...");

    let auth_data = poll_openai_authorization_code(device_auth_id, user_code, interval).await?;
    let authorization_code = auth_data
        .get("authorization_code")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let code_verifier = auth_data
        .get("code_verifier")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if authorization_code.is_empty() || code_verifier.is_empty() {
        bail!("Invalid authorization completion payload: {auth_data:?}");
    }

    let token_data = exchange_openai_authorization_code(authorization_code, code_verifier).await?;
    let access_token = token_data
        .get("access_token")
        .and_then(Value::as_str)
        .ok_or_else(|| anyhow!("No access token in response: {token_data:?}"))?
        .to_string();
    let refresh_token = token_data
        .get("refresh_token")
        .and_then(Value::as_str)
        .map(str::to_string);
    let expires_in = token_data
        .get("expires_in")
        .and_then(Value::as_f64)
        .unwrap_or(3600.0);
    let account_id = token_data
        .get("account_id")
        .and_then(Value::as_str)
        .map(str::to_string)
        .or_else(|| {
            token_data
                .get("id_token")
                .and_then(Value::as_str)
                .and_then(extract_openai_account_id)
        })
        .or_else(|| {
            token_data
                .get("access_token")
                .and_then(Value::as_str)
                .and_then(extract_openai_account_id)
        });

    write_openai_oauth_tokens(
        token_file,
        &OpenAIOAuthTokens {
            access_token: Some(access_token),
            refresh_token,
            expires_at: Some(unix_now_seconds() + expires_in),
            account_id,
            env_access_token: false,
        },
    )?;

    println!("OAuth tokens saved to {}", token_file.display());
    Ok(())
}

async fn start_openai_device_flow() -> Result<Value> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()?;
    let response = client
        .post(OPENAI_DEVICE_USERCODE_URL)
        .header("Content-Type", "application/json")
        .header("User-Agent", OPENAI_LOGIN_USER_AGENT)
        .json(&serde_json::json!({"client_id": OPENAI_CLIENT_ID}))
        .send()
        .await
        .context("starting OpenAI device flow")?;

    let status = response.status();
    let text = response
        .text()
        .await
        .context("reading device-flow response")?;
    if !status.is_success() {
        bail!("Device auth start failed: {} {}", status, text);
    }
    Ok(serde_json::from_str(&text).context("parsing device-flow response JSON")?)
}

async fn poll_openai_authorization_code(
    device_auth_id: &str,
    user_code: &str,
    interval_seconds: u64,
) -> Result<Value> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()?;
    let wait =
        Duration::from_secs(interval_seconds.max(1) + OPENAI_DEVICE_POLL_SAFETY_MARGIN_SECONDS);
    let deadline = Instant::now() + Duration::from_secs(OPENAI_DEVICE_AUTH_TIMEOUT_SECONDS);

    loop {
        let response = client
            .post(OPENAI_DEVICE_TOKEN_URL)
            .header("Content-Type", "application/json")
            .header("User-Agent", OPENAI_LOGIN_USER_AGENT)
            .json(&serde_json::json!({
                "device_auth_id": device_auth_id,
                "user_code": user_code,
            }))
            .send()
            .await
            .context("polling OpenAI device authorization")?;

        let status = response.status();
        let text = response
            .text()
            .await
            .context("reading device polling response")?;
        if status.is_success() {
            return Ok(serde_json::from_str(&text).context("parsing device polling JSON")?);
        }

        if matches!(status.as_u16(), 403 | 404) {
            if Instant::now() >= deadline {
                break;
            }
            tokio::time::sleep(wait).await;
            continue;
        }

        bail!("Device authorization polling failed: {} {}", status, text);
    }

    bail!(
        "Timed out waiting for OpenAI device authorization after {}s",
        OPENAI_DEVICE_AUTH_TIMEOUT_SECONDS
    )
}

async fn exchange_openai_authorization_code(
    authorization_code: &str,
    code_verifier: &str,
) -> Result<Value> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()?;
    let response = client
        .post(OPENAI_TOKEN_URL)
        .header("User-Agent", OPENAI_LOGIN_USER_AGENT)
        .form(&[
            ("grant_type", "authorization_code"),
            ("code", authorization_code),
            ("redirect_uri", OPENAI_DEVICE_REDIRECT_URI),
            ("client_id", OPENAI_CLIENT_ID),
            ("code_verifier", code_verifier),
        ])
        .send()
        .await
        .context("exchanging OpenAI authorization code")?;

    let status = response.status();
    let text = response
        .text()
        .await
        .context("reading token exchange response")?;
    if !status.is_success() {
        bail!("Token exchange failed: {} {}", status, text);
    }
    Ok(serde_json::from_str(&text).context("parsing token exchange JSON")?)
}

fn try_open_browser(url: &str) {
    #[cfg(target_os = "macos")]
    let _ = Command::new("open").arg(url).spawn();

    #[cfg(target_os = "windows")]
    let _ = Command::new("cmd").args(["/C", "start", "", url]).spawn();

    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    let _ = Command::new("xdg-open").arg(url).spawn();
}

// =============================================================================
// Model selection
// =============================================================================

fn prompt_models(provider: Provider) -> Result<(String, String)> {
    let catalog = model_catalog();
    let provider_entry = catalog.get(provider.id());

    let main_entries = provider_entry
        .and_then(|p| p.get("main"))
        .cloned()
        .unwrap_or_default();
    let aux_entries = provider_entry
        .and_then(|p| p.get("aux"))
        .cloned()
        .unwrap_or_default();

    println!("\nMain model (handles user-facing turns):");
    let main = pick_model("main", &main_entries)?;
    println!("\nAuxiliary model (cheap calls — summarization, heartbeat, background):");
    let aux = pick_model("aux", &aux_entries)?;
    Ok((main, aux))
}

fn pick_model(label: &str, entries: &[lethe::llm::models::ModelEntry]) -> Result<String> {
    if entries.is_empty() {
        let raw = prompt_line(&format!("  {label} model id: "))?;
        let raw = raw.trim();
        if raw.is_empty() {
            bail!("a model id is required");
        }
        return Ok(raw.to_string());
    }
    for (idx, entry) in entries.iter().enumerate() {
        println!(
            "  {}) {} — {} ({})",
            idx + 1,
            entry.name(),
            entry.model_id(),
            entry.price()
        );
    }
    let prompt = format!(
        "  Choose [1-{}, default=1, or type a custom id]: ",
        entries.len()
    );
    let answer = prompt_line(&prompt)?;
    let answer = answer.trim();
    if answer.is_empty() {
        return Ok(entries[0].model_id().to_string());
    }
    if let Ok(n) = answer.parse::<usize>()
        && (1..=entries.len()).contains(&n)
    {
        return Ok(entries[n - 1].model_id().to_string());
    }
    Ok(answer.to_string())
}

// =============================================================================
// API key
// =============================================================================

fn prompt_api_key(provider: Provider, existing_env: &EnvMap) -> Result<String> {
    let env_name = provider.key_env();
    let existing = env_value(env_name, existing_env);
    if let Some(key) = existing {
        println!("\nFound existing {env_name}: {}", mask_key(&key));
        let answer = prompt_line("Use it? [Y/n]: ")?;
        if !answer.trim().to_ascii_lowercase().starts_with('n') {
            return Ok(key);
        }
    }
    println!("\n{env_name} required.");
    println!("  Get one at: {}", provider.key_url());
    let key = prompt_line(&format!("  Paste {env_name}: "))?;
    let key = key.trim().to_string();
    if key.is_empty() {
        bail!("{env_name} is required to continue");
    }
    Ok(key)
}

fn mask_key(key: &str) -> String {
    let trimmed = key.trim();
    if trimmed.len() <= 12 {
        return "<short-key>".to_string();
    }
    let head: String = trimmed.chars().take(8).collect();
    let tail: String = trimmed
        .chars()
        .rev()
        .take(4)
        .collect::<String>()
        .chars()
        .rev()
        .collect();
    format!("{head}…{tail}")
}

// =============================================================================
// Optional Telegram
// =============================================================================

struct TelegramSetup {
    bot_token: String,
    allowed_user_ids: String,
}

fn prompt_telegram(existing_env: &EnvMap) -> Result<Option<TelegramSetup>> {
    println!("\nOptional: Telegram bot setup");
    println!("  Skip this if you only want CLI or HTTP API access.");
    let yes_no = prompt_line("  Configure Telegram now? [y/N]: ")?;
    if !yes_no.trim().to_ascii_lowercase().starts_with('y') {
        return Ok(None);
    }
    let existing_token = env_value("TELEGRAM_BOT_TOKEN", existing_env);
    let token = match existing_token.as_deref().filter(|v| !v.trim().is_empty()) {
        Some(value) => {
            println!("  Found existing TELEGRAM_BOT_TOKEN: {}", mask_key(value));
            let keep = prompt_line("  Use it? [Y/n]: ")?;
            if keep.trim().to_ascii_lowercase().starts_with('n') {
                prompt_line("  Paste new bot token: ")?.trim().to_string()
            } else {
                value.to_string()
            }
        }
        None => {
            println!("  Get a bot token from @BotFather (https://t.me/BotFather).");
            prompt_line("  Paste TELEGRAM_BOT_TOKEN: ")?
                .trim()
                .to_string()
        }
    };
    if token.is_empty() {
        return Ok(None);
    }
    let allowed = prompt_line("  Allowed Telegram user ids (comma-separated, or blank for any): ")?
        .trim()
        .to_string();
    Ok(Some(TelegramSetup {
        bot_token: token,
        allowed_user_ids: allowed,
    }))
}

// =============================================================================
// Optional human-block seed
// =============================================================================

fn prompt_human_intro() -> Result<Option<String>> {
    println!("\nOptional: tell Lethe about yourself");
    println!("  This seeds the `human` memory block — anything you want the assistant");
    println!("  to remember from turn one (your name, preferences, role). Leave blank to skip.");
    let answer = prompt_line("  > ")?;
    let answer = answer.trim();
    if answer.is_empty() {
        Ok(None)
    } else {
        Ok(Some(answer.to_string()))
    }
}

// =============================================================================
// Persistence
// =============================================================================

fn write_env_file(
    path: &Path,
    provider: Provider,
    main_model: &str,
    aux_model: &str,
    api_key: Option<&str>,
    openai_oauth_token_file: Option<&Path>,
    telegram: Option<&TelegramSetup>,
) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("creating config dir at {}", parent.display()))?;
    }
    let now = chrono::Utc::now().format("%Y-%m-%d %H:%M:%S %Z");
    let mut body = String::new();
    body.push_str(&format!(
        "# Lethe configuration — generated by `lethe init` on {now}\n\n"
    ));
    body.push_str(&format!("LLM_PROVIDER={}\n", provider.id()));
    body.push_str(&format!("LLM_MODEL={main_model}\n"));
    body.push_str(&format!("LLM_MODEL_AUX={aux_model}\n"));
    match (provider, api_key, openai_oauth_token_file) {
        (Provider::OpenAI, _, Some(token_file)) => {
            body.push_str(&format!(
                "LETHE_OPENAI_OAUTH_TOKENS={}\n\n",
                token_file.display()
            ));
        }
        (_, Some(key), _) => {
            body.push_str(&format!("{}={}\n\n", provider.key_env(), key));
        }
        _ => body.push('\n'),
    }
    if let Some(telegram) = telegram {
        body.push_str("# Telegram bot\n");
        body.push_str(&format!("TELEGRAM_BOT_TOKEN={}\n", telegram.bot_token));
        if !telegram.allowed_user_ids.is_empty() {
            body.push_str(&format!(
                "TELEGRAM_ALLOWED_USER_IDS={}\n",
                telegram.allowed_user_ids
            ));
        }
        body.push('\n');
    }
    body.push_str("# Add more knobs from .env.example (background subsystems, paths, etc.)\n");
    std::fs::write(path, body).with_context(|| format!("writing {}", path.display()))?;
    // Lock the file to user-read/write only — it contains secrets.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

fn seed_workspace(settings: &Settings) -> Result<()> {
    // MemoryStore::from_settings already creates directories + seeds blocks.
    // We re-instantiate here so init works even if no chat has run yet.
    let _ = MemoryStore::from_settings(settings)
        .with_context(|| "opening memory store under workspace")?;
    Ok(())
}

fn seed_human_block(settings: &Settings, text: &str) -> Result<()> {
    let blocks_dir = settings.paths.workspace_dir.join("memory");
    let manager = BlockManager::new(&blocks_dir)
        .with_context(|| format!("opening blocks dir {}", blocks_dir.display()))?;
    manager.init_embedded_defaults()?;
    manager
        .update("human", Some(text), None)
        .with_context(|| "writing seed text to human block")?;
    Ok(())
}

// =============================================================================
// Smoke test
// =============================================================================

async fn smoke_test(
    provider: Provider,
    main_model: &str,
    aux_model: &str,
    api_key: Option<&str>,
    openai_oauth_token_file: Option<&Path>,
) -> Result<()> {
    unsafe {
        std::env::remove_var("OPENAI_AUTH_TOKEN");
        std::env::remove_var("LETHE_OPENAI_OAUTH_TOKENS");
        std::env::remove_var(Provider::OpenRouter.key_env());
        std::env::remove_var(Provider::Anthropic.key_env());
        std::env::remove_var(Provider::OpenAI.key_env());

        if let Some(token_file) = openai_oauth_token_file {
            std::env::set_var("LETHE_OPENAI_OAUTH_TOKENS", token_file);
        }
        if let Some(api_key) = api_key {
            std::env::set_var(provider.key_env(), api_key);
        }
        std::env::set_var("LLM_PROVIDER", provider.id());
        std::env::set_var("LLM_MODEL", main_model);
        std::env::set_var("LLM_MODEL_AUX", aux_model);
    }
    let settings = Settings::from_env();
    let router = LlmRouter::new(LlmRouterConfig::from_settings(&settings));
    let probe = vec![
        LlmMessage::system("Reply with the single word: ok"),
        LlmMessage::user("ready?"),
    ];
    match router.complete(probe, true).await {
        Ok(reply) => {
            let preview = reply.trim().lines().next().unwrap_or("").to_string();
            info(&format!("LLM ping via aux model: `{preview}`"));
            Ok(())
        }
        Err(error) => {
            warn(&format!("LLM ping failed: {error}"));
            warn("Config was saved — fix the key/model and re-run `lethe check` to verify.");
            Ok(())
        }
    }
}

// =============================================================================
// I/O helpers
// =============================================================================

type EnvMap = std::collections::HashMap<String, String>;

fn read_existing_env(path: &Path) -> EnvMap {
    let Ok(content) = std::fs::read_to_string(path) else {
        return EnvMap::new();
    };
    content
        .lines()
        .filter_map(|line| {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                return None;
            }
            let (k, v) = line.split_once('=')?;
            Some((k.trim().to_string(), v.trim().trim_matches('"').to_string()))
        })
        .collect()
}

fn prompt_line(prompt: &str) -> Result<String> {
    print!("{prompt}");
    io::stdout().flush().ok();
    let stdin = io::stdin();
    let mut line = String::new();
    stdin
        .lock()
        .read_line(&mut line)
        .with_context(|| "reading stdin")?;
    Ok(line
        .trim_end_matches('\n')
        .trim_end_matches('\r')
        .to_string())
}

fn env_present(key: &str, existing_env: &EnvMap) -> bool {
    std::env::var(key)
        .ok()
        .is_some_and(|value| !value.trim().is_empty())
        || existing_env
            .get(key)
            .is_some_and(|value| !value.trim().is_empty())
}

fn env_value(key: &str, existing_env: &EnvMap) -> Option<String> {
    std::env::var(key)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .or_else(|| {
            existing_env
                .get(key)
                .cloned()
                .filter(|value| !value.trim().is_empty())
        })
}

fn unix_now_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

fn print_header() {
    println!("Lethe — guided setup");
    println!("--------------------\n");
}

fn info(message: &str) {
    println!("  → {message}");
}

fn success(message: &str) {
    println!("✓ {message}");
}

fn warn(message: &str) {
    println!("! {message}");
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn openai_subscription_env_file_writes_token_path_and_skips_api_key() {
        let tmp = tempdir().unwrap();
        let env_path = tmp.path().join(".env");
        let token_file = tmp.path().join("credentials/openai_oauth_tokens.json");
        write_env_file(
            &env_path,
            Provider::OpenAI,
            "gpt-5.3-codex",
            "gpt-5.3-codex",
            None,
            Some(&token_file),
            None,
        )
        .unwrap();
        let text = std::fs::read_to_string(&env_path).unwrap();
        assert!(text.contains("LETHE_OPENAI_OAUTH_TOKENS="));
        assert!(!text.contains("OPENAI_API_KEY="));
    }

    #[test]
    fn openai_subscription_model_must_be_available_in_provider_catalog() {
        assert!(validate_openai_subscription_model("gpt-5.5-codex").is_ok());
        assert!(validate_openai_subscription_model("openai/gpt-5.5-codex").is_ok());
        assert!(validate_openai_subscription_model("gpt-5.2").is_ok());
        assert!(validate_openai_subscription_model("gpt-future").is_err());
    }

    #[test]
    fn openai_subscription_aux_falls_back_to_main_when_needed() {
        assert_eq!(
            openai_subscription_aux_model("gpt-5.5-codex", "gpt-future"),
            "gpt-5.5-codex"
        );
        assert_eq!(
            openai_subscription_aux_model("gpt-5.5-codex", "gpt-5.5-codex"),
            "gpt-5.5-codex"
        );
    }
}
