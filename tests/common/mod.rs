use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;

pub(crate) fn build_id_token(email: &str, plan: &str) -> String {
    let header = serde_json::json!({
        "alg": "none",
        "typ": "JWT",
    });
    let auth = serde_json::json!({
        "chatgpt_plan_type": plan,
    });
    let payload = serde_json::json!({
        "email": email,
        "https://api.openai.com/auth": auth,
    });
    let header = URL_SAFE_NO_PAD.encode(serde_json::to_string(&header).unwrap());
    let payload = URL_SAFE_NO_PAD.encode(serde_json::to_string(&payload).unwrap());
    format!("{header}.{payload}.")
}
