import asyncio
import sys
import os
import time
import json
import secrets
from pathlib import Path
from typing import Optional, List, Dict, Any
import httpx

from fastapi import FastAPI, HTTPException, Request, Header, Depends, status
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Set UTF-8 output encoding for Windows command line
sys.stdout.reconfigure(encoding='utf-8')

# Ensure the package src directory is in the path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from gemini_webapi import GeminiClient, logger
from gemini_webapi.exceptions import AuthError, TemporarilyBlocked, UsageLimitExceeded
from gemini_webapi.types.image import WebImage, GeneratedImage
from gemini_webapi.types.gem import Gem
from gemini_webapi.constants import Model

try:
    from media_services import (
        build_image_prompt,
        build_video_prompt,
        build_web_search_prompt,
        extract_and_save_media,
        save_image_asset,
        save_video_asset,
        wait_and_collect_media,
    )
except ImportError:
    # Docker / flat layout
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from media_services import (
        build_image_prompt,
        build_video_prompt,
        build_web_search_prompt,
        extract_and_save_media,
        save_image_asset,
        save_video_asset,
        wait_and_collect_media,
    )

app = FastAPI(
    title="Gemini OpenAI-Compatible Web API",
    description="A production-ready Web API server wrapping the Gemini Web Client with OpenAI compatibility.",
    version="1.2.0"
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ensure static folder exists and mount it
static_dir = ROOT / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Official Google API Client Wrapper for Gemini API keys
class GoogleAPIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.status = "Active"

    @property
    def gems(self):
        return []

    async def fetch_gems(self, include_hidden=False):
        return []

    async def close(self):
        pass

    async def generate_content(self, prompt: str, chat=None, gem=None):
        model = "gemini-2.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}]
        }
        
        class MockResponse:
            def __init__(self, text, images=[]):
                self.text = text
                self.images = images
                
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.post(url, json=payload, timeout=30.0)
            resp.raise_for_status()
            data = resp.json()
            
            try:
                text = data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError) as e:
                logger.error(f"Failed to parse text from generateContent response: {data}, error: {e}")
                text = f"Error generating text. Full response: {data}"
                
            return MockResponse(text)

    async def generate_content_stream(self, prompt: str, chat=None, gem=None):
        model = "gemini-2.5-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?key={self.api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}]
        }
        
        class MockStreamChunk:
            def __init__(self, text_delta, images=[]):
                self.text_delta = text_delta
                self.images = images
                
        async with httpx.AsyncClient(verify=False) as client:
            async with client.stream("POST", url, json=payload, timeout=60.0) as response:
                response.raise_for_status()
                buffer = ""
                async for line in response.aiter_lines():
                    buffer += line.strip()
                    while True:
                        start_idx = buffer.find('{')
                        if start_idx == -1:
                            break
                        
                        brace_count = 0
                        end_idx = -1
                        for i in range(start_idx, len(buffer)):
                            char = buffer[i]
                            if char == '{':
                                brace_count += 1
                            elif char == '}':
                                brace_count -= 1
                                if brace_count == 0:
                                    end_idx = i
                                    break
                                    
                        if end_idx != -1:
                            obj_str = buffer[start_idx:end_idx+1]
                            buffer = buffer[end_idx+1:]
                            try:
                                obj_data = json.loads(obj_str)
                                text_delta = obj_data["candidates"][0]["content"]["parts"][0].get("text", "")
                                yield MockStreamChunk(text_delta)
                            except Exception:
                                pass
                        else:
                            break

    # generate_image method removed

# Default Fallback Credentials (empty by default for 24/7 — prefer Extension / providers JSON)
SECURE_1PSID = os.getenv("SECURE_1PSID", "").strip()
SECURE_1PSIDTS = os.getenv("SECURE_1PSIDTS", "").strip()
API_KEY = os.getenv("API_KEY", "")

# 24/7 ops knobs (env-overridable)
SESSION_MAX_COUNT = int(os.getenv("SESSION_MAX_COUNT", "500"))
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))  # 1h idle
ACCOUNT_COOLDOWN_SECONDS = int(os.getenv("ACCOUNT_COOLDOWN_SECONDS", "120"))
MAX_FAILOVER_ATTEMPTS = int(os.getenv("MAX_FAILOVER_ATTEMPTS", "3"))
WATCHDOG_INTERVAL_SECONDS = int(os.getenv("WATCHDOG_INTERVAL_SECONDS", "45"))
PROACTIVE_COOKIE_ON_EVERY_REQUEST = os.getenv("PROACTIVE_COOKIE_ON_EVERY_REQUEST", "1") != "0"

# JSON Storage Files
KEYS_FILE = ROOT / "api_keys.json"
ACCOUNTS_FILE = ROOT / "gemini_accounts.json"
AGENTS_FILE = ROOT / "gemini_agents.json"
CONFIG_FILE = ROOT / "dashboard_config.json"

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading dashboard_config.json: {e}")
    return {
        "dashboard_password": "123456",
        "disable_default_provider": False
    }

def save_config(config: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving dashboard_config.json: {e}")

# In-memory logs ring buffer for dashboard
api_logs = []
logs_lock = asyncio.Lock()

# Multi-account rotation pool and mapping variables
client_pool = {}
pool_lock = asyncio.Lock()
default_client: Optional[GeminiClient] = None
default_client_status = "Uninitialized"
default_requests_count = 0

rotation_index = 0
session_to_account = {}  # session_id -> account_id (or "default")
sessions = {}  # session_id -> ChatSession
session_last_used: Dict[str, float] = {}  # session_id -> unix ts
account_cooldown_until: Dict[str, float] = {}  # acc_id -> unix ts (skip after rate-limit/auth)
ops_stats: Dict[str, Any] = {
    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "failover_count": 0,
    "cookie_recoveries": 0,
    "proactive_cookie_updates": 0,
    "sessions_evicted": 0,
    "last_watchdog_at": None,
    "last_error": None,
    "images_generated": 0,
    "videos_generated": 0,
    "research_runs": 0,
    "media_jobs_completed": 0,
}

# Extension-assisted media download queue (when server cannot download with cookies)
media_jobs: Dict[str, Dict[str, Any]] = {}
media_jobs_lock = asyncio.Lock()
MEDIA_JOB_TTL = 600  # seconds

# Extension auth-helper bridge state (cookie auto-import)
extension_state: Dict[str, Any] = {
    "connected": False,
    "last_heartbeat": None,
    "last_cookie_sync": None,
    "ext_id": None,
    "provider_id": None,
    "provider_name": None,
    "masked_psid": None,
    "has_psid": False,
    "has_psidts": False,
    "last_error": None,
    "auto_apply": True,
    # Dashboard can request an immediate cookie pull from the extension
    "force_cookie_sync": False,
    "force_cookie_sync_until": 0.0,
    "force_cookie_sync_name": "Extension Auto",
}
# Temporary holding area for cookies pushed by the extension (not written to disk until applied)
_extension_cookie_cache: Dict[str, Any] = {
    "psid": None,
    "psidts": None,
    "name": None,
    "received_at": None,
    "applied_fingerprint": None,  # last fingerprint successfully applied to the pool
}
EXTENSION_PROVIDER_MARKER = "extension_auto"


def _cookie_pair_fingerprint(psid: str, psidts: str) -> str:
    return f"{psid or ''}||{psidts or ''}"

# Analytics tracking state
analytics_lock = asyncio.Lock()
analytics_data = {
    "total_requests": 0,
    "total_successes": 0,
    "total_errors": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "endpoints": {},  # path -> count
    "providers": {},  # provider_id -> count
    "time_series": []  # List of dicts: {"minute": "HH:MM", "requests": X, "tokens": Y}
}

# Pre-populate rolling time-series with the last 15 minutes
current_time = time.time()
for i in range(14, -1, -1):
    min_str = time.strftime("%H:%M", time.localtime(current_time - i * 60))
    analytics_data["time_series"].append({
        "minute": min_str,
        "requests": 0,
        "tokens": 0
    })

async def record_analytics_request(path: str, provider_id: str, success: bool, prompt_tokens: int = 0, completion_tokens: int = 0):
    async with analytics_lock:
        analytics_data["total_requests"] += 1
        if success:
            analytics_data["total_successes"] += 1
        else:
            analytics_data["total_errors"] += 1
            
        analytics_data["prompt_tokens"] += prompt_tokens
        analytics_data["completion_tokens"] += completion_tokens
        
        # Endpoint count
        analytics_data["endpoints"][path] = analytics_data["endpoints"].get(path, 0) + 1
        
        # Provider count
        analytics_data["providers"][provider_id] = analytics_data["providers"].get(provider_id, 0) + 1
        
        # Time-series updates
        current_min = time.strftime("%H:%M")
        
        # Find or create entry for current minute
        ts_entry = None
        for entry in analytics_data["time_series"]:
            if entry["minute"] == current_min:
                ts_entry = entry
                break
                
        if ts_entry:
            ts_entry["requests"] += 1
            ts_entry["tokens"] += (prompt_tokens + completion_tokens)
        else:
            analytics_data["time_series"].append({
                "minute": current_min,
                "requests": 1,
                "tokens": prompt_tokens + completion_tokens
            })
            
        # Limit time_series to last 15 minutes
        if len(analytics_data["time_series"]) > 15:
            analytics_data["time_series"].pop(0)

# Middleware for logging traffic
@app.middleware("http")
async def log_requests(request: Request, call_next):
    path = request.url.path
    is_api_request = path.startswith("/v1/")
    
    if is_api_request:
        # Create log entry beforehand so that the endpoint/stream generator can reference and update it in-place
        client_ip = request.client.host if request.client else "127.0.0.1"
        log_entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "method": request.method,
            "path": path,
            "status_code": 0, # populated after route executes
            "duration": "0.00s",
            "ip": client_ip,
            "tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "agent": "-"
        }
        request.state.log_entry = log_entry
        
        start_time = time.time()
        response = await call_next(request)
        duration = time.time() - start_time
        
        log_entry["status_code"] = response.status_code
        log_entry["duration"] = f"{duration:.2f}s"
        
        async with logs_lock:
            api_logs.append(log_entry)
            if len(api_logs) > 50:
                api_logs.pop(0)
                
        # Record non-streaming analytics
        is_streaming = "text/event-stream" in response.headers.get("content-type", "")
        if not is_streaming:
            provider_id = getattr(request.state, "provider_id", "default")
            prompt_tokens = getattr(request.state, "prompt_tokens", 0)
            completion_tokens = getattr(request.state, "completion_tokens", 0)
            
            # Sync tokens back to log entry
            log_entry["prompt_tokens"] = prompt_tokens
            log_entry["completion_tokens"] = completion_tokens
            log_entry["tokens"] = prompt_tokens + completion_tokens
            
            success = (200 <= response.status_code < 400)
            await record_analytics_request(path, provider_id, success, prompt_tokens, completion_tokens)
        else:
            # For streaming, we pre-fill prompt tokens which are already calculated in the route handler
            prompt_tokens = getattr(request.state, "prompt_tokens", 0)
            log_entry["prompt_tokens"] = prompt_tokens
            log_entry["tokens"] = prompt_tokens
            
        return response
    else:
        return await call_next(request)

# API Models
class ChatMessage(BaseModel):
    role: str
    content: Optional[Any] = ""

def get_message_text(msg: ChatMessage) -> str:
    content = msg.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text" and "text" in part:
                    text_parts.append(str(part["text"]))
                elif "text" in part:
                    text_parts.append(str(part["text"]))
            elif isinstance(part, str):
                text_parts.append(part)
        return "\n".join(text_parts)
    return str(content)


class ChatCompletionRequest(BaseModel):
    model: str = "gemini"
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    max_tokens: Optional[int] = None
    user: Optional[str] = None

class CreateKeyPayload(BaseModel):
    name: str
    agent_id: Optional[str] = None

class CreateProviderPayload(BaseModel):
    name: str
    provider_type: Optional[str] = "web" # "web" or "api_key"
    psid: Optional[str] = ""
    psidts: Optional[str] = ""
    api_key: Optional[str] = ""

class ExtensionGeminiCookiesPayload(BaseModel):
    """Cookies pushed from the Chrome auth-helper extension."""
    psid: Optional[str] = ""
    psidts: Optional[str] = ""
    # Alternate field names the extension may send
    secure_1psid: Optional[str] = None
    secure_1psidts: Optional[str] = None
    name: Optional[str] = "Extension Auto"
    email: Optional[str] = None
    auto_apply: Optional[bool] = True
    cookies: Optional[Dict[str, str]] = None  # raw cookie map fallback

class CreateAgentPayload(BaseModel):
    name: str
    description: Optional[str] = ""
    system_prompt: str
    base_model: Optional[str] = "gemini"
    temperature: Optional[float] = 1.0


def _mask_secret(value: str, head: int = 8, tail: int = 6) -> str:
    if not value:
        return "None"
    if len(value) <= head + tail:
        return value[:4] + "..." if len(value) > 4 else "****"
    return f"{value[:head]}...{value[-tail:]}"


def _extract_psid_pair(payload: ExtensionGeminiCookiesPayload) -> tuple[str, str]:
    """Normalize PSID / PSIDTS from various extension payload shapes."""
    psid = (payload.psid or payload.secure_1psid or "").strip()
    psidts = (payload.psidts or payload.secure_1psidts or "").strip()
    if payload.cookies and isinstance(payload.cookies, dict):
        cmap = payload.cookies
        if not psid:
            psid = (
                cmap.get("__Secure-1PSID")
                or cmap.get("Secure-1PSID")
                or cmap.get("psid")
                or ""
            ).strip()
        if not psidts:
            psidts = (
                cmap.get("__Secure-1PSIDTS")
                or cmap.get("Secure-1PSIDTS")
                or cmap.get("psidts")
                or ""
            ).strip()
    return psid, psidts


def is_cookie_auth_failure(exc: BaseException) -> bool:
    """True when the error indicates invalid/expired Gemini web cookies/session."""
    if isinstance(exc, AuthError):
        return True
    if isinstance(exc, TemporarilyBlocked):
        # IP block is not cookie death, but provider should cool down
        return False
    msg = str(exc).lower()
    needles = (
        "autherror",
        "auth error",
        "authentication",
        "unauthorized",
        "not logged",
        "please login",
        "sign in",
        "invalid cookie",
        "cookie",
        "__secure-1psid",
        "1psid",
        "401",
        "403",
        "session expired",
        "credentials",
    )
    return any(n in msg for n in needles)


async def request_fresh_cookies_from_extension(
    reason: str,
    name: str = "Extension Auto",
    ttl: int = 180,
) -> None:
    """Ask the Chrome extension (next heartbeat / bridge) to push fresh browser cookies."""
    extension_state["force_cookie_sync"] = True
    extension_state["force_cookie_sync_until"] = time.time() + max(30, ttl)
    extension_state["force_cookie_sync_name"] = name or "Extension Auto"
    extension_state["last_error"] = reason
    logger.warning(f"[cookie-recovery] Requesting fresh cookies from extension: {reason}")


async def apply_newer_extension_cookies_if_changed() -> dict:
    """
    Proactive update (NO need to wait for a failed request):

    If the extension has pushed cookies into `_extension_cookie_cache` that differ
    from what the active extension provider is using (or provider is dead/missing),
    re-init the provider immediately.

    Returns a small status dict for logging / optional response headers.
    """
    cached_psid = _extension_cookie_cache.get("psid")
    if not cached_psid:
        return {"updated": False, "reason": "no_cache"}

    cached_psidts = _extension_cookie_cache.get("psidts") or ""
    cache_fp = _cookie_pair_fingerprint(cached_psid, cached_psidts)
    name = _extension_cookie_cache.get("name") or extension_state.get("provider_name") or "Extension Auto"

    accounts = load_accounts()
    ext = next((a for a in accounts if a.get("source") == EXTENSION_PROVIDER_MARKER), None)

    if ext:
        live_fp = _cookie_pair_fingerprint(ext.get("psid") or "", ext.get("psidts") or "")
        pool_info = client_pool.get(ext["id"]) or {}
        pool_active = pool_info.get("status") == "Active" and pool_info.get("client") is not None
        if live_fp == cache_fp and pool_active:
            return {"updated": False, "reason": "already_current", "fingerprint": cache_fp[:24]}
        # Cookies differ OR provider not healthy → update now (before next chat fails)
        reason = "fingerprint_diff" if live_fp != cache_fp else "provider_not_active"
    else:
        reason = "no_extension_provider"

    try:
        result = await upsert_extension_provider(
            cached_psid,
            cached_psidts,
            name,
            force_reinit=True,
        )
        _extension_cookie_cache["applied_fingerprint"] = cache_fp
        extension_state["force_cookie_sync"] = False
        extension_state["last_error"] = None
        logger.info(
            f"[cookie-proactive] Updated provider from extension cache "
            f"(reason={reason}, id={result.get('id')})"
        )
        return {
            "updated": True,
            "reason": reason,
            "provider_id": result.get("id"),
            "unchanged": bool(result.get("unchanged")),
        }
    except Exception as e:
        logger.warning(f"[cookie-proactive] Failed to apply newer cookies: {e}")
        return {"updated": False, "reason": "apply_failed", "error": str(e)}


async def mark_account_auth_failed(acc_id: str, error: BaseException) -> None:
    """
    Mark a provider as Disconnected after cookie/auth failure and request extension re-sync
    when the account is extension-managed (or when the whole pool is down).
    """
    global default_client_status
    err_str = str(error)
    cool_secs = ACCOUNT_COOLDOWN_SECONDS
    if isinstance(error, TemporarilyBlocked):
        cool_secs = max(cool_secs, 300)  # rate-limit: longer cool-down
        # Don't permanently kill pool entry on temporary block — just cool down
        set_account_cooldown(acc_id, cool_secs)
        logger.warning(f"[24/7] Provider {acc_id} cooled down {cool_secs}s (rate limit/block)")
        return

    set_account_cooldown(acc_id, cool_secs)
    async with pool_lock:
        if acc_id == "default":
            default_client_status = "Disconnected"
        elif acc_id in client_pool:
            client_pool[acc_id]["status"] = "Disconnected"
            client_pool[acc_id]["error"] = err_str

        # Drop sticky sessions bound to this dead account so next request can rotate
        dead_sessions = [sid for sid, aid in session_to_account.items() if aid == acc_id]
        for sid in dead_sessions:
            del session_to_account[sid]
            if sid in sessions:
                del sessions[sid]
            session_last_used.pop(sid, None)

    ops_stats["cookie_recoveries"] = int(ops_stats.get("cookie_recoveries") or 0) + 1
    ops_stats["last_error"] = err_str

    # Decide whether to pull new cookies from extension
    accounts = load_accounts()
    acc = next((a for a in accounts if a.get("id") == acc_id), None)
    is_extension = bool(acc and acc.get("source") == EXTENSION_PROVIDER_MARKER)

    any_active = bool(list_active_provider_ids(exclude={acc_id}))

    if is_extension or not any_active or acc_id == "default":
        display = (acc or {}).get("name") or extension_state.get("provider_name") or "Extension Auto"
        await request_fresh_cookies_from_extension(
            f"Auth/cookie failure on provider {acc_id}: {err_str}",
            name=display,
            ttl=180,
        )


async def upsert_extension_provider(psid: str, psidts: str, name: str, force_reinit: bool = False) -> dict:
    """
    Create or refresh the dedicated 'extension_auto' web provider in the pool.
    Reuses the same account id so sticky sessions stay valid when cookies rotate.
    Skips full re-init when cookies are unchanged and the client is already Active
    (unless force_reinit / recovery after auth failure).
    """
    accounts = load_accounts()
    existing = next((a for a in accounts if a.get("source") == EXTENSION_PROVIDER_MARKER), None)
    display_name = name or "Extension Auto"

    # Recovery path: always re-init when force sync was requested after auth failure
    recovering = force_reinit or bool(extension_state.get("force_cookie_sync"))

    # Fast path: same cookies + healthy pool client → no re-init
    if existing and not recovering:
        acc_id = existing["id"]
        same_cookies = (
            (existing.get("psid") or "") == psid
            and (existing.get("psidts") or "") == (psidts or "")
        )
        pool_info = client_pool.get(acc_id) or {}
        if (
            same_cookies
            and pool_info.get("status") == "Active"
            and pool_info.get("client") is not None
        ):
            extension_state["provider_id"] = acc_id
            extension_state["provider_name"] = existing.get("name") or display_name
            extension_state["masked_psid"] = _mask_secret(psid)
            extension_state["has_psid"] = bool(psid)
            extension_state["has_psidts"] = bool(psidts)
            extension_state["last_error"] = None
            extension_state["last_cookie_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _extension_cookie_cache["applied_fingerprint"] = _cookie_pair_fingerprint(psid, psidts or "")
            return {
                "id": acc_id,
                "name": extension_state["provider_name"],
                "status": "Active",
                "unchanged": True,
            }

    cl = await init_single_client(psid, psidts)

    if existing:
        acc_id = existing["id"]
        existing["name"] = display_name
        existing["psid"] = psid
        existing["psidts"] = psidts
        existing["provider_type"] = "web"
        existing["source"] = EXTENSION_PROVIDER_MARKER
        # keep requests_count
        save_accounts(accounts)
    else:
        acc_id = secrets.token_hex(4)
        new_acc = {
            "id": acc_id,
            "name": display_name,
            "provider_type": "web",
            "psid": psid,
            "psidts": psidts,
            "requests_count": 0,
            "source": EXTENSION_PROVIDER_MARKER,
        }
        accounts.append(new_acc)
        save_accounts(accounts)

    async with pool_lock:
        # Close previous client if any
        old = client_pool.get(acc_id)
        if old and old.get("client") and old["client"] is not cl:
            try:
                await old["client"].close()
            except Exception:
                pass
        client_pool[acc_id] = {
            "client": cl,
            "name": display_name,
            "status": "Active",
            "requests_count": (old or {}).get("requests_count", 0) if old else 0,
            "error": None,
            "source": EXTENSION_PROVIDER_MARKER,
        }

    extension_state["provider_id"] = acc_id
    extension_state["provider_name"] = display_name
    extension_state["masked_psid"] = _mask_secret(psid)
    extension_state["has_psid"] = bool(psid)
    extension_state["has_psidts"] = bool(psidts)
    extension_state["last_error"] = None
    extension_state["last_cookie_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
    extension_state["force_cookie_sync"] = False
    _extension_cookie_cache["applied_fingerprint"] = _cookie_pair_fingerprint(psid, psidts or "")

    return {"id": acc_id, "name": display_name, "status": "Active", "unchanged": False}

# API Key persistence helpers
def load_keys() -> List[Dict[str, Any]]:
    if not KEYS_FILE.exists():
        return []
    try:
        with open(KEYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load keys from file: {e}")
        return []

def save_keys(keys_list: List[Dict[str, Any]]):
    try:
        with open(KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(keys_list, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save keys to file: {e}")

# Gemini Accounts persistent helper
def load_accounts() -> List[Dict[str, Any]]:
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load accounts: {e}")
        return []

def save_accounts(accounts_list: List[Dict[str, Any]]):
    try:
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts_list, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save accounts: {e}")

# Custom AI Agents persistent helper
def load_custom_agents() -> List[Dict[str, Any]]:
    if not AGENTS_FILE.exists():
        return []
    try:
        with open(AGENTS_FILE, "r", encoding="utf-8") as f:
            agents = json.load(f)
            # Ensure every agent has an API key in the agent object and in api_keys.json
            keys_list = load_keys()
            keys_modified = False
            agents_modified = False
            for a in agents:
                agent_id = a["id"]
                if "api_key" not in a:
                    import secrets
                    a["api_key"] = f"sk-{agent_id}-{secrets.token_hex(12)}"
                    agents_modified = True
                
                # Verify this key (or any key for this agent) is in api_keys.json
                has_key_in_db = any(k.get("key") == a["api_key"] or k.get("agent_id") == agent_id for k in keys_list)
                if not has_key_in_db:
                    import secrets
                    new_key_entry = {
                        "id": secrets.token_hex(4),
                        "name": f"Default Key for {a['name']}",
                        "key": a["api_key"],
                        "agent_id": agent_id,
                        "created_at": a.get("created_at") or time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    keys_list.append(new_key_entry)
                    keys_modified = True
            
            if agents_modified:
                save_custom_agents(agents)
            if keys_modified:
                save_keys(keys_list)
            return agents
    except Exception as e:
        logger.error(f"Failed to load custom agents: {e}")
        return []

def save_custom_agents(agents_list: List[Dict[str, Any]]):
    try:
        with open(AGENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(agents_list, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save custom agents: {e}")

# API Key dependency validation
async def verify_api_key(request: Request, authorization: Optional[str] = Header(None)):
    keys_list = load_keys()
    
    # Enforce if there is at least one key in database, or env key
    enforced = len(keys_list) > 0 or bool(API_KEY)
    
    request.state.is_general_key = True
    request.state.authenticated_agent_id = None
    request.state.token = None
    
    if enforced:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key (Bearer token required)"
            )
        token = authorization.split(" ")[1]
        request.state.token = token
        
        # Check against environmental variable first
        if API_KEY and token == API_KEY:
            return
            
        # Check against database keys
        for key_entry in keys_list:
            if token == key_entry["key"]:
                agent_id = key_entry.get("agent_id")
                if agent_id:
                    request.state.is_general_key = False
                    request.state.authenticated_agent_id = agent_id
                else:
                    request.state.is_general_key = True
                return
                
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )

# Single client initializer helper
async def init_single_client(psid: str, psidts: str) -> GeminiClient:
    cl = GeminiClient(psid, psidts, verify=False)
    await cl.init(timeout=45, auto_close=False, auto_refresh=True)
    try:
        await cl.fetch_gems(include_hidden=False)
    except Exception as e:
        logger.warning(f"Failed to prefetch Gems during connection test: {e}")
    return cl

# Pool initializer
async def initialize_client_pool():
    global default_client, default_client_status
    accounts = load_accounts()
    
    config = load_config()
    disable_default = config.get("disable_default_provider", False)
    # 24/7: skip broken hard-coded fallback when no env cookies
    if not SECURE_1PSID:
        disable_default = True
    
    if disable_default:
        logger.info("Fallback default GeminiClient is disabled (config or empty SECURE_1PSID).")
        default_client_status = "Disconnected"
        default_client = None
    else:
        logger.info("Initializing fallback default GeminiClient...")
        default_client = GeminiClient(SECURE_1PSID, SECURE_1PSIDTS, verify=False)
        try:
            await default_client.init(timeout=45, auto_close=False, auto_refresh=True)
            await default_client.fetch_gems(include_hidden=False)
            default_client_status = "Active"
            logger.info("Fallback default GeminiClient initialized successfully!")
        except Exception as e:
            default_client_status = "Disconnected"
            logger.error(f"Failed to initialize default GeminiClient: {e}")
        
    for acc in accounts:
        acc_id = acc["id"]
        ptype = acc.get("provider_type", "web")
        if ptype == "api_key":
            logger.info(f"Initializing GoogleAPIClient for account {acc['name']} ({acc_id})...")
            cl = GoogleAPIClient(acc.get("api_key", ""))
            client_pool[acc_id] = {
                "client": cl,
                "name": acc["name"],
                "status": "Active",
                "requests_count": acc.get("requests_count", 0),
                "error": None,
                "source": acc.get("source"),
            }
            logger.info(f"GoogleAPIClient for {acc['name']} connected!")
        else:
            logger.info(f"Initializing GeminiClient for account {acc['name']} ({acc_id})...")
            try:
                cl = await init_single_client(acc["psid"], acc["psidts"])
                client_pool[acc_id] = {
                    "client": cl,
                    "name": acc["name"],
                    "status": "Active",
                    "requests_count": acc.get("requests_count", 0),
                    "error": None,
                    "source": acc.get("source"),
                }
                logger.info(f"GeminiClient for {acc['name']} connected!")
            except Exception as e:
                client_pool[acc_id] = {
                    "client": None,
                    "name": acc["name"],
                    "status": "Disconnected",
                    "requests_count": acc.get("requests_count", 0),
                    "error": str(e),
                    "source": acc.get("source"),
                }
                logger.error(f"Failed to connect client for {acc['name']}: {e}")
                if acc.get("source") == EXTENSION_PROVIDER_MARKER:
                    # Will also be requested in startup seed; mark intent early
                    extension_state["force_cookie_sync"] = True
                    extension_state["force_cookie_sync_until"] = time.time() + 180

def _is_account_cooled(acc_id: str) -> bool:
    until = float(account_cooldown_until.get(acc_id) or 0)
    return time.time() < until


def set_account_cooldown(acc_id: str, seconds: Optional[int] = None) -> None:
    sec = seconds if seconds is not None else ACCOUNT_COOLDOWN_SECONDS
    account_cooldown_until[acc_id] = time.time() + max(10, sec)


def touch_session(session_id: str) -> None:
    session_last_used[session_id] = time.time()


def cleanup_stale_sessions() -> int:
    """Evict idle / excess chat sessions to keep memory stable for 24/7."""
    now = time.time()
    evicted = 0
    # TTL eviction
    stale = [
        sid for sid, ts in list(session_last_used.items())
        if (now - float(ts)) > SESSION_TTL_SECONDS
    ]
    for sid in stale:
        sessions.pop(sid, None)
        session_to_account.pop(sid, None)
        session_last_used.pop(sid, None)
        evicted += 1
    # Cap total sessions (oldest first)
    if len(sessions) > SESSION_MAX_COUNT:
        ordered = sorted(session_last_used.items(), key=lambda x: x[1])
        overflow = len(sessions) - SESSION_MAX_COUNT
        for sid, _ in ordered[:overflow]:
            sessions.pop(sid, None)
            session_to_account.pop(sid, None)
            session_last_used.pop(sid, None)
            evicted += 1
    if evicted:
        ops_stats["sessions_evicted"] = int(ops_stats.get("sessions_evicted") or 0) + evicted
        logger.info(f"[24/7] Evicted {evicted} chat sessions (ttl/cap).")
    return evicted


def list_active_provider_ids(exclude: Optional[set] = None) -> List[str]:
    """Active providers not in cooldown / exclude set."""
    exclude = exclude or set()
    now = time.time()
    active: List[str] = []
    for aid, info in client_pool.items():
        if aid in exclude:
            continue
        if info.get("status") != "Active" or not info.get("client"):
            continue
        if now < float(account_cooldown_until.get(aid) or 0):
            continue
        active.append(aid)
    config = load_config()
    disable_default = config.get("disable_default_provider", False)
    if (
        not disable_default
        and default_client
        and default_client_status == "Active"
        and "default" not in exclude
        and SECURE_1PSID
        and now >= float(account_cooldown_until.get("default") or 0)
    ):
        active.append("default")
    return active


# Client selector rotation logic with sticky session + failover exclude
async def get_client_for_session(
    session_id: str,
    exclude: Optional[set] = None,
    force_rotate: bool = False,
) -> tuple[Optional[GeminiClient], str]:
    global rotation_index, default_client_status
    exclude = exclude or set()
    touch_session(session_id)

    async with pool_lock:
        # 1. Sticky Session Lookup (unless force_rotate or excluded/cooled)
        acc_id = None if force_rotate else session_to_account.get(session_id)
        if acc_id and acc_id not in exclude and not _is_account_cooled(acc_id):
            if acc_id == "default":
                config = load_config()
                disable_default = config.get("disable_default_provider", False)
                if (
                    not disable_default
                    and default_client_status == "Active"
                    and default_client
                    and SECURE_1PSID
                ):
                    return default_client, "default"
            elif acc_id in client_pool and client_pool[acc_id]["status"] == "Active":
                return client_pool[acc_id]["client"], acc_id

        # 2. Round-robin among healthy providers
        active_ids = [
            aid for aid, info in client_pool.items()
            if info.get("status") == "Active"
            and info.get("client")
            and aid not in exclude
            and not _is_account_cooled(aid)
        ]

        if active_ids:
            selected_id = active_ids[rotation_index % len(active_ids)]
            rotation_index += 1
            session_to_account[session_id] = selected_id
            return client_pool[selected_id]["client"], selected_id

        # 3. Fallback default env client
        config = load_config()
        disable_default = config.get("disable_default_provider", False)
        if (
            not disable_default
            and default_client
            and SECURE_1PSID
            and "default" not in exclude
            and not _is_account_cooled("default")
        ):
            session_to_account[session_id] = "default"
            if default_client_status != "Active":
                try:
                    await default_client.init(timeout=45, auto_close=False, auto_refresh=True)
                    default_client_status = "Active"
                except Exception:
                    return None, "default"
            if default_client_status == "Active":
                return default_client, "default"

        return None, "none"

# Helper to save image locally
async def save_image_locally(img) -> str:
    static_path = ROOT / "static"
    static_path.mkdir(parents=True, exist_ok=True)
    fn = await save_image_asset(img, static_path)
    if not fn:
        # fallback original path
        kwargs = {}
        if isinstance(img, GeneratedImage):
            kwargs["full_size"] = True
        abs_path_str = await img.save(path=str(static_path), verbose=True, **kwargs)
        return Path(abs_path_str).name
    return fn


async def enqueue_media_jobs(pending: List[Dict[str, Any]]) -> List[str]:
    """Queue media URLs for the Chrome extension to download with browser cookies."""
    job_ids = []
    if not pending:
        return job_ids
    async with media_jobs_lock:
        # purge expired
        now = time.time()
        for jid in list(media_jobs.keys()):
            if now - float(media_jobs[jid].get("created_at", 0)) > MEDIA_JOB_TTL:
                media_jobs.pop(jid, None)
        for item in pending:
            jid = secrets.token_hex(6)
            media_jobs[jid] = {
                "id": jid,
                "media_type": item.get("media_type") or "video",
                "source_url": item.get("source_url"),
                "thumbnail_url": item.get("thumbnail_url"),
                "title": item.get("title") or "",
                "status": "pending",
                "filename": None,
                "public_url": None,
                "error": None,
                "created_at": now,
            }
            job_ids.append(jid)
    return job_ids


async def get_active_gemini_client() -> tuple[Any, str]:
    """Pick any healthy client for one-shot media/research jobs."""
    cl, acc_id = await get_client_for_session(f"media-{secrets.token_hex(4)}", force_rotate=True)
    if not cl:
        await request_fresh_cookies_from_extension("media/research needs provider", ttl=90)
        await apply_newer_extension_cookies_if_changed()
        cl, acc_id = await get_client_for_session(f"media-{secrets.token_hex(4)}", force_rotate=True)
    return cl, acc_id

# Usage count increment helper
def increment_request_count(acc_id: str):
    global default_requests_count
    if acc_id == "default":
        default_requests_count += 1
        return
        
    if acc_id in client_pool:
        client_pool[acc_id]["requests_count"] += 1
        
    accounts = load_accounts()
    for acc in accounts:
        if acc["id"] == acc_id:
            acc["requests_count"] = acc.get("requests_count", 0) + 1
            break
    save_accounts(accounts)

# Client-specific Gems fetching helper
async def get_client_gems(cl: GeminiClient) -> List[Any]:
    if not cl:
        return []
    try:
        return cl.gems
    except RuntimeError:
        logger.info("Gems cache is empty. Fetching gems from client...")
        await cl.fetch_gems(include_hidden=False)
        return cl.gems

# Client-specific Gem resolving helper
async def resolve_gem_for_client(cl: GeminiClient, model_name: str) -> Optional[Gem]:
    gems = await get_client_gems(cl)
    target_id = model_name
    if target_id.startswith("gem-"):
        target_id = target_id[4:]
    
    for gem in gems:
        if gem.id == target_id:
            return gem
            
    for gem in gems:
        if gem.name.lower() == model_name.lower():
            return gem
            
    return None

async def cookie_health_watchdog():
    """
    24/7 background job:
    - Evict stale chat sessions (memory)
    - Apply newer extension cookies if fingerprint differs
    - Request extension re-sync when providers are down
    - Clear expired cooldowns
    """
    while True:
        try:
            await asyncio.sleep(max(20, WATCHDOG_INTERVAL_SECONDS))
            ops_stats["last_watchdog_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

            # Memory hygiene
            cleanup_stale_sessions()

            # Proactive apply of any newer cookies sitting in cache
            try:
                result = await apply_newer_extension_cookies_if_changed()
                if result.get("updated"):
                    ops_stats["proactive_cookie_updates"] = int(
                        ops_stats.get("proactive_cookie_updates") or 0
                    ) + 1
            except Exception as e:
                logger.warning(f"Watchdog proactive cookie apply: {e}")

            # Drop expired cooldowns
            now = time.time()
            for aid in list(account_cooldown_until.keys()):
                if now >= float(account_cooldown_until.get(aid) or 0):
                    account_cooldown_until.pop(aid, None)

            any_active = False
            if default_client_status == "Active" and default_client and SECURE_1PSID:
                any_active = True
            extension_dead = False
            for acc_id, info in list(client_pool.items()):
                if info.get("status") == "Active" and info.get("client"):
                    any_active = True
                if info.get("source") == EXTENSION_PROVIDER_MARKER and info.get("status") != "Active":
                    extension_dead = True
            for acc in load_accounts():
                if acc.get("source") != EXTENSION_PROVIDER_MARKER:
                    continue
                st = (client_pool.get(acc["id"]) or {}).get("status")
                if st != "Active":
                    extension_dead = True

            # Heartbeat age: if extension silent > 3 min while we need it, still request cookies
            ext_quiet = True
            last_hb = extension_state.get("last_heartbeat")
            if last_hb:
                try:
                    hb_ts = time.mktime(time.strptime(last_hb, "%Y-%m-%d %H:%M:%S"))
                    ext_quiet = (time.time() - hb_ts) > 180
                except Exception:
                    ext_quiet = True

            if extension_dead or not any_active:
                until = float(extension_state.get("force_cookie_sync_until") or 0)
                if time.time() > until - 20:
                    await request_fresh_cookies_from_extension(
                        "Watchdog 24/7: provider offline — pull browser cookies",
                        name=extension_state.get("provider_name") or "Extension Auto",
                        ttl=150,
                    )
                    ops_stats["cookie_recoveries"] = int(ops_stats.get("cookie_recoveries") or 0) + 1
            elif not ext_quiet:
                # Soft keep-alive: ensure need_cookie_sync false but fingerprint stay warm
                # (extension still heartbeats; nothing to do)
                pass

        except asyncio.CancelledError:
            raise
        except Exception as e:
            ops_stats["last_error"] = str(e)
            logger.warning(f"cookie_health_watchdog error: {e}")


@app.on_event("startup")
async def startup_event():
    logger.info("Initializing Gemini account connection pool...")
    await initialize_client_pool()
    # Seed extension bridge state from any persisted extension_auto provider
    for acc in load_accounts():
        if acc.get("source") == EXTENSION_PROVIDER_MARKER:
            acc_id = acc["id"]
            pool_info = client_pool.get(acc_id) or {}
            extension_state["provider_id"] = acc_id
            extension_state["provider_name"] = acc.get("name") or "Extension Auto"
            extension_state["has_psid"] = bool(acc.get("psid"))
            extension_state["has_psidts"] = bool(acc.get("psidts"))
            extension_state["masked_psid"] = _mask_secret(acc.get("psid") or "")
            if pool_info.get("status") == "Active":
                extension_state["last_cookie_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
            else:
                # Provider failed on boot → ask extension for fresh cookies immediately
                await request_fresh_cookies_from_extension(
                    f"Startup: extension provider {acc_id} not Active",
                    name=acc.get("name") or "Extension Auto",
                    ttl=180,
                )
            logger.info(
                f"Extension auto provider ready: {acc.get('name')} ({acc_id}) "
                f"status={pool_info.get('status', 'unknown')}"
            )
            break

    # Start background cookie recovery watcher
    asyncio.create_task(cookie_health_watchdog())
    logger.info("Cookie health watchdog started (checks every 60s).")

@app.on_event("shutdown")
async def shutdown_event():
    # Close default client
    if default_client:
        try:
            await default_client.close()
            logger.info("Fallback default GeminiClient closed.")
        except Exception:
            pass
            
    # Close pool clients
    async with pool_lock:
        for acc_id, info in client_pool.items():
            cl = info.get("client")
            if cl:
                try:
                    await cl.close()
                    logger.info(f"GeminiClient for {info['name']} closed.")
                except Exception:
                    pass

# Dashboard HTML page serving
@app.get("/")
@app.get("/overview")
@app.get("/analytics")
@app.get("/providers")
@app.get("/keys")
@app.get("/agents")
@app.get("/traffic")
@app.get("/guide")
async def get_dashboard():
    index_path = ROOT / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found.")
    return FileResponse(str(index_path))


@app.get("/chat-test")
@app.get("/playground")
async def get_playground():
    """Interactive chat + image + video test page for the API."""
    path = ROOT / "playground.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="playground.html not found.")
    return FileResponse(str(path))

# Dashboard API endpoints
@app.get("/api/status")
async def get_api_status():
    global default_client_status
    
    # Resolve host IP address
    import socket
    host_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        host_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
        
    config = load_config()
    disable_default = config.get("disable_default_provider", False)
    
    active_providers_count = 0
    if not disable_default and default_client_status == "Active":
        active_providers_count += 1
    for info in client_pool.values():
        if info.get("status") == "Active":
            active_providers_count += 1
            
    total_providers = len(client_pool)
    if not disable_default:
        total_providers += 1
        
    return {
        "host_ip": host_ip,
        "port": 8000,
        "keys_count": len(load_keys()),
        "active_providers_count": active_providers_count,
        "total_providers_count": total_providers,
        "extension": {
            "connected": extension_state.get("connected", False),
            "last_heartbeat": extension_state.get("last_heartbeat"),
            "last_cookie_sync": extension_state.get("last_cookie_sync"),
            "provider_id": extension_state.get("provider_id"),
            "provider_name": extension_state.get("provider_name"),
            "masked_psid": extension_state.get("masked_psid"),
            "has_psid": extension_state.get("has_psid", False),
            "has_psidts": extension_state.get("has_psidts", False),
            "last_error": extension_state.get("last_error"),
        },
    }

# ---------------------------------------------------------------------------
# Chrome Extension Auth Helper bridge
# Extension posts to these /sync/* routes (no dashboard token required).
# Point the extension "Local Backend Port" to 8000 (this server).
# ---------------------------------------------------------------------------

def _mark_extension_heartbeat(request: Request):
    extension_state["connected"] = True
    extension_state["last_heartbeat"] = time.strftime("%Y-%m-%d %H:%M:%S")
    ext_id = request.headers.get("X-Ext-Id")
    if ext_id:
        extension_state["ext_id"] = ext_id


@app.get("/sync/status")
@app.post("/sync/status")
async def extension_sync_status(request: Request):
    """Heartbeat endpoint used by extension-auth-helper."""
    _mark_extension_heartbeat(request)

    # Resolve whether extension should (re)push the current browser session cookies
    provider_id = extension_state.get("provider_id")
    provider_active = False
    if provider_id and provider_id in client_pool:
        provider_active = client_pool[provider_id].get("status") == "Active"
    elif provider_id is None:
        # Recover provider id from persisted accounts (after server restart)
        for acc in load_accounts():
            if acc.get("source") == EXTENSION_PROVIDER_MARKER:
                provider_id = acc["id"]
                extension_state["provider_id"] = provider_id
                extension_state["provider_name"] = acc.get("name") or "Extension Auto"
                pool_info = client_pool.get(provider_id) or {}
                provider_active = pool_info.get("status") == "Active"
                break

    force_until = float(extension_state.get("force_cookie_sync_until") or 0)
    force_active = bool(extension_state.get("force_cookie_sync")) and time.time() < force_until
    if extension_state.get("force_cookie_sync") and time.time() >= force_until:
        extension_state["force_cookie_sync"] = False

    need_cookie_sync = (
        force_active
        or (not provider_id)
        or (not provider_active)
        or (not extension_state.get("last_cookie_sync"))
    )

    return {
        "status": "ok",
        "service": "gemini-api",
        "cookie_sync": True,
        "need_cookie_sync": need_cookie_sync,
        "force_cookie_sync": force_active,
        "preferred_name": extension_state.get("force_cookie_sync_name") or "Extension Auto",
        "last_cookie_sync": extension_state.get("last_cookie_sync"),
        "provider_id": provider_id,
        "provider_active": provider_active,
        "auto_cookie_sync": True,
    }


@app.get("/sync/theme")
@app.post("/sync/theme")
async def extension_sync_theme(request: Request):
    """
    Compatibility stub for the Bawui/Labs extension loop.
    Returns an empty theme payload so the extension keeps heartbeating
    while we still receive Gemini cookies on /sync/gemini-cookies.
    """
    _mark_extension_heartbeat(request)
    return {"d": None, "r": None, "g": 0, "x": None}


@app.get("/sync/config")
async def extension_sync_config(request: Request):
    _mark_extension_heartbeat(request)
    return {
        "service": "gemini-api",
        "cookie_sync_path": "/sync/gemini-cookies",
        "recaptcha_ent_key": "",
        "recaptcha_action": "",
    }


@app.post("/sync/render")
@app.post("/sync/google-one-activity")
@app.post("/sync/google-flow-page")
@app.post("/sync/grok-event")
@app.post("/sync/grok-poll-task")
async def extension_sync_noop(request: Request):
    """Accept (and ignore) other extension payloads so the bridge stays green."""
    _mark_extension_heartbeat(request)
    return {"status": "ok", "ignored": True}


@app.post("/sync/gemini-cookies")
async def extension_push_gemini_cookies(
    payload: ExtensionGeminiCookiesPayload,
    request: Request,
):
    """
    Receive __Secure-1PSID / __Secure-1PSIDTS from the Chrome extension
    and optionally auto-apply them as a web provider in the pool.
    """
    _mark_extension_heartbeat(request)
    psid, psidts = _extract_psid_pair(payload)

    if not psid:
        extension_state["last_error"] = "Missing __Secure-1PSID cookie"
        raise HTTPException(
            status_code=400,
            detail="Missing __Secure-1PSID. Open gemini.google.com and log in first.",
        )

    # Cache for dashboard manual apply + proactive compare-before-request
    incoming_fp = _cookie_pair_fingerprint(psid, psidts or "")
    previous_fp = _extension_cookie_cache.get("applied_fingerprint")
    cookies_changed = bool(previous_fp and previous_fp != incoming_fp)

    _extension_cookie_cache["psid"] = psid
    _extension_cookie_cache["psidts"] = psidts or ""
    _extension_cookie_cache["name"] = payload.name or "Extension Auto"
    _extension_cookie_cache["received_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    extension_state["has_psid"] = True
    extension_state["has_psidts"] = bool(psidts)
    extension_state["masked_psid"] = _mask_secret(psid)
    extension_state["last_cookie_sync"] = _extension_cookie_cache["received_at"]

    auto_apply = True if payload.auto_apply is None else bool(payload.auto_apply)
    extension_state["auto_apply"] = auto_apply

    if not auto_apply:
        logger.info("Extension cookies cached (auto_apply=false). Waiting for dashboard apply.")
        return {
            "status": "cached",
            "message": "Cookies received. Apply from Dashboard → Providers.",
            "masked_psid": extension_state["masked_psid"],
            "has_psidts": bool(psidts),
            "cookies_changed": cookies_changed,
        }

    try:
        # Prefer dashboard-requested display name when force-sync is active
        preferred_name = payload.name or extension_state.get("force_cookie_sync_name") or "Extension Auto"
        recovering = bool(extension_state.get("force_cookie_sync"))
        # Proactive: different fingerprint → always re-init (don't wait for auth error)
        result = await upsert_extension_provider(
            psid,
            psidts or "",
            preferred_name,
            force_reinit=recovering or cookies_changed,
        )
        extension_state["force_cookie_sync"] = False
        extension_state["last_error"] = None
        logger.info(
            f"Extension cookies applied as provider: {result['name']} ({result['id']}) "
            f"recovering={recovering} unchanged={result.get('unchanged')}"
        )
        return {
            "status": "success",
            "message": (
                "Cookies unchanged; provider still Active."
                if result.get("unchanged")
                else "Cookies applied and provider is Active."
            ),
            "provider": result,
            "unchanged": bool(result.get("unchanged")),
            "masked_psid": extension_state["masked_psid"],
            "has_psidts": bool(psidts),
        }
    except Exception as e:
        extension_state["last_error"] = str(e)
        logger.error(f"Failed to apply extension cookies: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Cookies received but connection test failed: {e}",
        )


@app.get("/api/extension/status")
async def get_extension_status():
    """Dashboard view of extension connectivity and last cookie sync."""
    # Consider disconnected if no heartbeat in the last 2 minutes
    connected = False
    last_hb = extension_state.get("last_heartbeat")
    if last_hb:
        try:
            hb_ts = time.mktime(time.strptime(last_hb, "%Y-%m-%d %H:%M:%S"))
            connected = (time.time() - hb_ts) < 120
        except Exception:
            connected = bool(extension_state.get("connected"))
    extension_state["connected"] = connected

    return {
        "connected": connected,
        "last_heartbeat": last_hb,
        "last_cookie_sync": extension_state.get("last_cookie_sync"),
        "ext_id": extension_state.get("ext_id"),
        "provider_id": extension_state.get("provider_id"),
        "provider_name": extension_state.get("provider_name"),
        "masked_psid": extension_state.get("masked_psid"),
        "has_psid": extension_state.get("has_psid", False),
        "has_psidts": extension_state.get("has_psidts", False),
        "has_cached_cookies": bool(_extension_cookie_cache.get("psid")),
        "last_error": extension_state.get("last_error"),
        "auto_cookie_sync": True,
        "hint": (
            "Extension tự động lấy cookie tài khoản Gemini đang login trong Chrome "
            "(heartbeat ~15s, cookie change, mở gemini.google.com). "
            "Port backend = 8000. Không cần copy cookie thủ công."
        ),
    }


@app.post("/api/extension/apply-cached")
async def apply_cached_extension_cookies():
    """Manually apply the last cookies received from the extension (dashboard button)."""
    psid = _extension_cookie_cache.get("psid")
    psidts = _extension_cookie_cache.get("psidts") or ""
    name = _extension_cookie_cache.get("name") or "Extension Auto"
    if not psid:
        raise HTTPException(
            status_code=400,
            detail="No cookies cached from extension yet. Sync from the extension first.",
        )
    try:
        result = await upsert_extension_provider(psid, psidts, name)
        return {"status": "success", "provider": result}
    except Exception as e:
        extension_state["last_error"] = str(e)
        raise HTTPException(status_code=400, detail=str(e))


class ExtensionRequestSyncPayload(BaseModel):
    name: Optional[str] = "Extension Auto"
    timeout_seconds: Optional[int] = 45


@app.post("/api/extension/request-sync")
async def request_extension_cookie_sync(payload: ExtensionRequestSyncPayload = ExtensionRequestSyncPayload()):
    """
    Dashboard asks the extension (via next heartbeat / dashboard bridge) to pull
    cookies from the currently logged-in Gemini account and auto-add provider.
    """
    name = (payload.name or "Extension Auto").strip() or "Extension Auto"
    timeout = max(10, min(int(payload.timeout_seconds or 45), 120))

    extension_state["force_cookie_sync"] = True
    extension_state["force_cookie_sync_until"] = time.time() + timeout
    extension_state["force_cookie_sync_name"] = name
    extension_state["last_error"] = None

    return {
        "status": "requested",
        "message": (
            "Waiting for extension to push cookies of the current Gemini session. "
            "Keep Chrome open with Gemini logged in."
        ),
        "name": name,
        "timeout_seconds": timeout,
        "need_cookie_sync": True,
    }


@app.post("/api/extension/import-provider")
async def import_provider_from_extension(payload: ExtensionRequestSyncPayload = ExtensionRequestSyncPayload()):
    """
    High-level dashboard action:
    1) Request extension cookie sync
    2) If cookies already cached, apply immediately
    3) Return current extension/provider status for the UI to poll
    """
    name = (payload.name or "Extension Auto").strip() or "Extension Auto"

    # Prefer already-cached cookies from a recent extension push
    cached_psid = _extension_cookie_cache.get("psid")
    cached_psidts = _extension_cookie_cache.get("psidts") or ""
    if cached_psid:
        try:
            result = await upsert_extension_provider(cached_psid, cached_psidts, name)
            extension_state["force_cookie_sync"] = False
            return {
                "status": "success",
                "source": "cache",
                "provider": result,
                "message": "Provider added from extension cookie cache.",
            }
        except Exception as e:
            extension_state["last_error"] = str(e)
            # Fall through to request a fresh pull

    extension_state["force_cookie_sync"] = True
    extension_state["force_cookie_sync_until"] = time.time() + max(
        10, min(int(payload.timeout_seconds or 45), 120)
    )
    extension_state["force_cookie_sync_name"] = name

    # If extension already has an active provider, re-label / confirm
    provider_id = extension_state.get("provider_id")
    if provider_id and provider_id in client_pool and client_pool[provider_id].get("status") == "Active":
        return {
            "status": "success",
            "source": "existing",
            "provider": {
                "id": provider_id,
                "name": extension_state.get("provider_name") or name,
                "status": "Active",
            },
            "message": "Extension provider already Active.",
        }

    return {
        "status": "pending",
        "source": "waiting_extension",
        "message": (
            "Requested cookie pull from extension. "
            "Dashboard bridge or extension heartbeat will add the provider shortly."
        ),
        "need_cookie_sync": True,
        "name": name,
    }


# API Keys Management Endpoints
@app.get("/api/keys")
async def get_api_keys():
    keys_list = load_keys()
    agents = load_custom_agents()
    agent_map = {a["id"]: a["name"] for a in agents}
    
    masked_list = []
    for entry in keys_list:
        k = entry["key"]
        masked_k = f"{k[:10]}...{k[-6:]}" if len(k) > 16 else "invalid-key"
        
        agent_id = entry.get("agent_id")
        agent_name = agent_map.get(agent_id) if agent_id else None
        
        masked_list.append({
            "id": entry.get("id"),
            "name": entry.get("name", "Unnamed Key"),
            "key": k,
            "masked_key": masked_k,
            "created_at": entry.get("created_at"),
            "agent_id": agent_id,
            "agent_name": agent_name
        })
    return masked_list

@app.post("/api/keys")
async def create_api_key(payload: CreateKeyPayload):
    keys_list = load_keys()
    
    agent_id = payload.agent_id or None
    if agent_id:
        # Validate that the agent exists
        agents = load_custom_agents()
        agent_exists = any(a["id"] == agent_id for a in agents)
        if not agent_exists:
            raise HTTPException(status_code=404, detail="AI Agent not found")
        new_key_val = f"sk-{agent_id}-{secrets.token_hex(12)}"
    else:
        new_key_val = f"sk-gemini-{secrets.token_hex(16)}"
        
    key_id = secrets.token_hex(4)
    new_entry = {
        "id": key_id,
        "name": payload.name or f"Key-{key_id}",
        "key": new_key_val,
        "agent_id": agent_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    keys_list.append(new_entry)
    save_keys(keys_list)
    return new_entry

@app.delete("/api/keys/{key_id}")
async def delete_api_key(key_id: str):
    keys_list = load_keys()
    filtered_keys = [entry for entry in keys_list if entry.get("id") != key_id]
    if len(filtered_keys) == len(keys_list):
        raise HTTPException(status_code=404, detail="API Key not found")
    save_keys(filtered_keys)
    return {"status": "success"}

# Providers (Gemini Accounts) Management Endpoints
@app.get("/api/providers")
async def get_providers():
    providers_list = []
    
    config = load_config()
    disable_default = config.get("disable_default_provider", False)
    
    # 1. Add Default Fallback Provider info
    if not disable_default:
        providers_list.append({
            "id": "default",
            "name": "Default Fallback (Env/Cookies)",
            "status": default_client_status,
            "requests_count": default_requests_count,
            "masked_psid": f"{SECURE_1PSID[:8]}...{SECURE_1PSID[-6:]}" if len(SECURE_1PSID) > 15 else "None",
            "is_default": True,
            "error": None
        })
    
    # 2. Add Custom Pool Providers info
    accounts = load_accounts()
    for acc in accounts:
        acc_id = acc["id"]
        pool_info = client_pool.get(acc_id, {})
        ptype = acc.get("provider_type", "web")
        if ptype == "api_key":
            api_key = acc.get("api_key", "")
            masked_psid = f"{api_key[:8]}...{api_key[-6:]}" if len(api_key) > 12 else "None"
        else:
            psid = acc.get("psid", "")
            masked_psid = f"{psid[:8]}...{psid[-6:]}" if len(psid) > 15 else "None"
        
        providers_list.append({
            "id": acc_id,
            "name": acc["name"],
            "provider_type": ptype,
            "status": pool_info.get("status", "Disconnected"),
            "requests_count": pool_info.get("requests_count", 0),
            "masked_psid": masked_psid,
            "is_default": False,
            "error": pool_info.get("error"),
            "source": acc.get("source") or pool_info.get("source"),
        })
    return providers_list

@app.post("/api/providers")
async def add_provider(payload: CreateProviderPayload):
    logger.info(f"Testing new provider connection for: {payload.name} (type: {payload.provider_type})...")
    try:
        acc_id = secrets.token_hex(4)
        ptype = payload.provider_type or "web"
        
        if ptype == "api_key":
            api_key = payload.api_key or ""
            if not api_key:
                raise Exception("API Key is required for Google AI Studio provider.")
            
            # Make a test call to verify API key
            test_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
            test_payload = {"contents": [{"parts": [{"text": "Hello"}]}]}
            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.post(test_url, json=test_payload, timeout=10.0)
                if resp.status_code != 200:
                    raise Exception(f"API returned status {resp.status_code}: {resp.text}")
            
            cl = GoogleAPIClient(api_key)
            new_acc = {
                "id": acc_id,
                "name": payload.name,
                "provider_type": "api_key",
                "api_key": api_key,
                "requests_count": 0
            }
        else:
            cl = await init_single_client(payload.psid, payload.psidts)
            new_acc = {
                "id": acc_id,
                "name": payload.name,
                "provider_type": "web",
                "psid": payload.psid,
                "psidts": payload.psidts,
                "requests_count": 0
            }
            
        accounts = load_accounts()
        accounts.append(new_acc)
        save_accounts(accounts)
        
        async with pool_lock:
            client_pool[acc_id] = {
                "client": cl,
                "name": payload.name,
                "status": "Active",
                "requests_count": 0,
                "error": None
            }
        logger.info(f"Successfully added active provider connection: {payload.name}")
        return {"status": "success", "id": acc_id}
    except Exception as e:
        logger.error(f"Failed to add provider connection: {e}")
        raise HTTPException(status_code=400, detail=f"Connection test failed: {str(e)}")

@app.delete("/api/providers/{account_id}")
async def delete_provider(account_id: str):
    if account_id == "default":
        config = load_config()
        config["disable_default_provider"] = True
        save_config(config)
        
        global default_client, default_client_status
        async with pool_lock:
            if default_client:
                try:
                    await default_client.close()
                except Exception:
                    pass
                default_client = None
            default_client_status = "Disconnected"
            
            # Clean sticky session mapping to default
            keys_to_del = [sid for sid, aid in session_to_account.items() if aid == "default"]
            for sid in keys_to_del:
                del session_to_account[sid]
                if sid in sessions:
                    del sessions[sid]
        return {"status": "success"}
        
    accounts = load_accounts()
    filtered = [acc for acc in accounts if acc["id"] != account_id]
    if len(filtered) == len(accounts):
        raise HTTPException(status_code=404, detail="Provider not found")
    save_accounts(filtered)
    
    async with pool_lock:
        if account_id in client_pool:
            info = client_pool[account_id]
            cl = info.get("client")
            if cl:
                try:
                    await cl.close()
                except Exception:
                    pass
            del client_pool[account_id]
            
    # Clean sticky session mapping to this provider
    keys_to_del = [sid for sid, aid in session_to_account.items() if aid == account_id]
    for sid in keys_to_del:
        del session_to_account[sid]
        if sid in sessions:
            del sessions[sid]
            
    return {"status": "success"}

# Traffic logs
@app.get("/api/traffic")
async def get_api_traffic():
    async with logs_lock:
        return list(reversed(api_logs))

# Analytics logs
@app.get("/api/analytics")
async def get_api_analytics():
    async with analytics_lock:
        # Calculate success rate
        total = analytics_data["total_requests"]
        successes = analytics_data["total_successes"]
        success_rate = (successes / total * 100) if total > 0 else 100.0
        
        # Build mapping of provider name for UI display
        resolved_providers = {}
        for pid, count in analytics_data["providers"].items():
            if pid == "default":
                resolved_providers["Default Fallback"] = count
            elif pid in client_pool:
                resolved_providers[client_pool[pid]["name"]] = count
            else:
                resolved_providers[f"Unknown ({pid})"] = count
                
        return {
            "total_requests": total,
            "total_successes": successes,
            "total_errors": analytics_data["total_errors"],
            "success_rate": round(success_rate, 1),
            "prompt_tokens": analytics_data["prompt_tokens"],
            "completion_tokens": analytics_data["completion_tokens"],
            "total_tokens": analytics_data["prompt_tokens"] + analytics_data["completion_tokens"],
            "endpoints": analytics_data["endpoints"],
            "providers": resolved_providers,
            "time_series": analytics_data["time_series"]
        }

# Custom AI Agents Management Endpoints
@app.get("/api/agents")
async def get_agents():
    return load_custom_agents()

@app.post("/api/agents")
async def create_agent(payload: CreateAgentPayload):
    agents = load_custom_agents()
    
    # Generate unique ID and API Key for agent
    agent_id = f"agent-{secrets.token_hex(4)}"
    agent_key = f"sk-{agent_id}-{secrets.token_hex(12)}"
    
    new_agent = {
        "id": agent_id,
        "api_key": agent_key,
        "name": payload.name,
        "description": payload.description or "",
        "system_prompt": payload.system_prompt,
        "base_model": payload.base_model or "gemini",
        "temperature": payload.temperature if payload.temperature is not None else 1.0,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    
    agents.append(new_agent)
    save_custom_agents(agents)
    
    # Register the default key in api_keys.json instantly
    keys_list = load_keys()
    new_key_entry = {
        "id": secrets.token_hex(4),
        "name": f"Default Key for {payload.name}",
        "key": agent_key,
        "agent_id": agent_id,
        "created_at": new_agent["created_at"]
    }
    keys_list.append(new_key_entry)
    save_keys(keys_list)
    
    return new_agent

@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str):
    agents = load_custom_agents()
    filtered = [a for a in agents if a["id"] != agent_id]
    if len(filtered) == len(agents):
        raise HTTPException(status_code=404, detail="Agent not found")
    save_custom_agents(filtered)
    
    # Also delete associated keys in api_keys.json
    keys_list = load_keys()
    filtered_keys = [k for k in keys_list if k.get("agent_id") != agent_id]
    save_keys(filtered_keys)
    
    return {"status": "success"}

@app.put("/api/agents/{agent_id}")
async def update_agent(agent_id: str, payload: CreateAgentPayload):
    agents = load_custom_agents()
    agent_idx = next((i for i, a in enumerate(agents) if a["id"] == agent_id), None)
    if agent_idx is None:
        raise HTTPException(status_code=404, detail="Agent not found")
        
    agents[agent_idx]["name"] = payload.name
    agents[agent_idx]["description"] = payload.description or ""
    agents[agent_idx]["system_prompt"] = payload.system_prompt
    agents[agent_idx]["base_model"] = payload.base_model or "gemini"
    agents[agent_idx]["temperature"] = payload.temperature if payload.temperature is not None else 1.0
    
    key_generated = False
    if "api_key" not in agents[agent_idx]:
        agents[agent_idx]["api_key"] = f"sk-{agent_id}-{secrets.token_hex(12)}"
        key_generated = True
        
    save_custom_agents(agents)
    
    if key_generated:
        keys_list = load_keys()
        new_key_entry = {
            "id": secrets.token_hex(4),
            "name": f"Default Key for {payload.name}",
            "key": agents[agent_idx]["api_key"],
            "agent_id": agent_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        keys_list.append(new_key_entry)
        save_keys(keys_list)
        
    return agents[agent_idx]

@app.get("/api/base-models")
async def get_base_models():
    models = ["gemini"]
    active_cl = default_client
    if default_client_status != "Active" or not active_cl:
        async with pool_lock:
            for info in client_pool.values():
                if info["status"] == "Active" and info["client"]:
                    active_cl = info["client"]
                    break
    if active_cl:
        try:
            gems = await get_client_gems(active_cl)
            for gem in gems:
                models.append(f"gem-{gem.id}")
        except Exception:
            pass
    return models

# OpenAI-Compatible API Endpoints
@app.get("/health")
async def health_check():
    """Docker / load-balancer health. 200 if process is up; degraded when no providers."""
    global default_client_status
    active_ids = list_active_provider_ids()
    active_pool_clients = any(
        info.get("status") == "Active" and info.get("client")
        for info in client_pool.values()
    )
    default_ok = default_client_status == "Active" and bool(SECURE_1PSID)
    healthy = default_ok or active_pool_clients or bool(active_ids)

    # Liveness: process always answers 200 if running (so Docker doesn't kill mid-recovery)
    # Readiness signal is in `ready` field.
    body = {
        "status": "healthy" if healthy else "degraded",
        "ready": healthy,
        "detail": (
            "Connected to Gemini client pool"
            if healthy
            else "No active providers — waiting for Extension cookies / accounts"
        ),
        "active_providers": len(active_ids),
        "sessions": len(sessions),
        "extension_connected": bool(extension_state.get("connected")),
        "extension_last_heartbeat": extension_state.get("last_heartbeat"),
        "force_cookie_sync": bool(extension_state.get("force_cookie_sync")),
        "ops": {
            "started_at": ops_stats.get("started_at"),
            "failover_count": ops_stats.get("failover_count"),
            "cookie_recoveries": ops_stats.get("cookie_recoveries"),
            "proactive_cookie_updates": ops_stats.get("proactive_cookie_updates"),
            "sessions_evicted": ops_stats.get("sessions_evicted"),
            "last_watchdog_at": ops_stats.get("last_watchdog_at"),
        },
        "uptime_mode": "24/7",
    }
    # Always 200 for liveness so container restarts don't flap during cookie recovery
    return body


@app.get("/ready")
async def readiness_check():
    """Strict readiness: 503 until at least one provider is Active."""
    active_ids = list_active_provider_ids()
    if active_ids:
        return {"status": "ready", "active_providers": len(active_ids)}
    return JSONResponse(
        status_code=503,
        content={
            "status": "not_ready",
            "detail": "No active Gemini providers. Install/login Extension or add accounts.",
            "force_cookie_sync": bool(extension_state.get("force_cookie_sync")),
        },
    )

@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
@app.get("/models", dependencies=[Depends(verify_api_key)])
async def list_models(request: Request):
    is_general_key = getattr(request.state, "is_general_key", True)
    authenticated_agent_id = getattr(request.state, "authenticated_agent_id", None)
    
    models = []
    
    if is_general_key:
        models.extend([
            {
                "id": "gemini",
                "object": "model",
                "created": 1686935002,
                "owned_by": "google",
                "description": "Standard Google Gemini Model (text + tools)",
            },
            {
                "id": "gemini-image",
                "object": "model",
                "created": 1686935002,
                "owned_by": "google",
                "description": "Image generation via Gemini Web (returns /static URLs)",
            },
            {
                "id": "gemini-video",
                "object": "model",
                "created": 1686935002,
                "owned_by": "google",
                "description": "Video generation via Gemini Web (returns /static URLs)",
            },
            {
                "id": "gemini-research",
                "object": "model",
                "created": 1686935002,
                "owned_by": "google",
                "description": "Deep research / web lookup via Gemini",
            },
            {
                "id": "gemini-web-search",
                "object": "model",
                "created": 1686935002,
                "owned_by": "google",
                "description": "Quick web search style Q&A via Gemini tools",
            },
        ])
        
        # Resolve models using the first available active client in the pool
        active_cl = default_client
        acc_id = "default"
        if default_client_status != "Active" or not active_cl:
            async with pool_lock:
                for aid, info in client_pool.items():
                    if info["status"] == "Active" and info["client"]:
                        active_cl = info["client"]
                        acc_id = aid
                        break
                        
        if active_cl:
            try:
                gems = await get_client_gems(active_cl)
                for gem in gems:
                    models.append({
                        "id": f"gem-{gem.id}",
                        "object": "model",
                        "created": 1686935002,
                        "owned_by": "google",
                        "description": gem.description or f"Custom Gem: {gem.name}",
                        "display_name": gem.name
                    })
            except Exception as e:
                logger.warning(f"Failed to list Gems for models endpoint: {e}")
                
    # Load and append custom AI agents
    try:
        agents = load_custom_agents()
        for agent in agents:
            if not is_general_key:
                if agent["id"] != authenticated_agent_id:
                    continue
            models.append({
                "id": agent["id"],
                "object": "model",
                "created": 1686935002,
                "owned_by": "custom-agent",
                "description": agent.get("description", f"Custom AI Agent: {agent['name']}"),
                "display_name": agent["name"]
            })
    except Exception as e:
        logger.warning(f"Failed to list custom agents for models endpoint: {e}")
            
    return {"object": "list", "data": models}

@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
@app.post("/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(payload: ChatCompletionRequest, request: Request):
    # Proactive: if Extension already pushed newer cookies than the active provider, apply NOW
    if PROACTIVE_COOKIE_ON_EVERY_REQUEST:
        try:
            proactive = await apply_newer_extension_cookies_if_changed()
            if proactive.get("updated"):
                request.state.cookie_proactive_update = True
                ops_stats["proactive_cookie_updates"] = int(
                    ops_stats.get("proactive_cookie_updates") or 0
                ) + 1
        except Exception as e:
            logger.warning(f"Proactive cookie check failed: {e}")

    # Get sticky or rotated active client
    session_id = request.headers.get("X-Session-ID")
    if not session_id:
        session_id = request.query_params.get("session_id")
    if not session_id:
        session_id = payload.user or "default_session"
        
    # Check if a reset is requested
    force_reset = request.headers.get("X-Reset") == "true" or request.query_params.get("reset") == "true"
    if force_reset or len(payload.messages) <= 1:
        if session_id in sessions:
            logger.info(f"Resetting chat session: {session_id}")
            del sessions[session_id]
            if session_id in session_to_account:
                del session_to_account[session_id]
            session_last_used.pop(session_id, None)
                
    # Select client (with multi-provider failover loop later on errors)
    cl, acc_id = await get_client_for_session(session_id)
    if not cl:
        # Last chance: ask extension + try proactive apply
        await request_fresh_cookies_from_extension(
            "No active providers at chat request",
            ttl=120,
        )
        try:
            await apply_newer_extension_cookies_if_changed()
        except Exception:
            pass
        cl, acc_id = await get_client_for_session(session_id, force_rotate=True)
    if not cl:
        raise HTTPException(
            status_code=503,
            detail=(
                "No active Gemini providers. "
                "Keep Chrome logged into gemini.google.com with the Auth Helper extension (port 8000)."
            ),
        )
    touch_session(session_id)
         
    # Set request state for telemetry
    request.state.provider_id = acc_id
    prompt_tokens = max(1, sum(len(get_message_text(m)) for m in payload.messages) // 4)
    request.state.prompt_tokens = prompt_tokens
    request.state.completion_tokens = 0
         
    # Resolve Gem/Model or Custom Agent
    gem_obj = None
    agent_system_prompt = None
    target_model = payload.model
    
    is_general_key = getattr(request.state, "is_general_key", True)
    authenticated_agent_id = getattr(request.state, "authenticated_agent_id", None)
    
    # 1. If authenticated with an Agent Key, they can ONLY call their own agent model
    if not is_general_key:
        if not payload.model or payload.model != authenticated_agent_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This API key is only authorized to call its associated AI Agent."
            )
            
    # 2. If calling a custom agent (model starts with "agent-"), they MUST use that agent's specific API key
    if payload.model and payload.model.startswith("agent-"):
        if is_general_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Access denied. This AI Agent requires its specific API Key."
            )
        # Resolve custom agent
        try:
            agents = load_custom_agents()
            agent_data = next((a for a in agents if a["id"] == payload.model), None)
            if not agent_data:
                raise HTTPException(status_code=404, detail="AI Agent not found")
                
            agent_system_prompt = agent_data.get("system_prompt")
            target_model = agent_data.get("base_model", "gemini")
            if agent_data.get("temperature") is not None:
                payload.temperature = agent_data["temperature"]
            
            # Set the agent name in telemetry log entry
            if hasattr(request.state, "log_entry"):
                request.state.log_entry["agent"] = agent_data["name"]
                
            logger.info(f"Using Custom AI Agent: {agent_data['name']} ({payload.model})")
        except HTTPException as he:
            raise he
        except Exception as e:
            logger.error(f"Error loading custom agent details: {e}")
            
    if target_model and target_model != "gemini":
        gem_obj = await resolve_gem_for_client(cl, target_model)
        if not gem_obj:
            logger.warning(f"Model/Gem '{target_model}' not found on active client. Defaulting to standard model.")
            
    # Get or create ChatSession
    if session_id not in sessions:
        sessions[session_id] = cl.start_chat()
        logger.info(f"Created new ChatSession for: {session_id} on client: {acc_id}")
        is_first_turn = True
    else:
        is_first_turn = False
        
    chat_session = sessions[session_id]
    
    # Extract system instruction
    system_content = None
    if payload.messages and payload.messages[0].role == "system":
        system_content = get_message_text(payload.messages[0])
        
    # Inject agent's system prompt if available
    if agent_system_prompt:
        if system_content:
            system_content = f"Agent Guidelines:\n{agent_system_prompt}\n\nClient instructions:\n{system_content}"
        else:
            system_content = agent_system_prompt
        
    # Find last user message
    user_msg = ""
    for msg in reversed(payload.messages):
        if msg.role == "user":
            user_msg = get_message_text(msg)
            break
            
    if not user_msg:
        raise HTTPException(status_code=400, detail="No user message found in the payload.")
        
    # Handle system message prepending
    prompt_to_send = user_msg
    if system_content:
        prompt_to_send = f"{system_content}\n\n{user_msg}"

    # Multi-modal model aliases (chat-compatible)
    # IMPORTANT: plain "gemini" / flash / pro = TEXT chat only (no forced image prompt)
    model_lower = (payload.model or "gemini").lower().strip()
    force_gemini_model = None  # Model enum passed to generate_content
    if model_lower in ("gemini-image", "image", "dall-e-3", "dall-e-2"):
        prompt_to_send = build_image_prompt(user_msg)
        force_gemini_model = Model.BASIC_FLASH
    elif model_lower in ("gemini-video", "video"):
        prompt_to_send = build_video_prompt(user_msg)
        force_gemini_model = Model.BASIC_FLASH
    elif model_lower in ("gemini-web-search", "web-search", "web_search", "search"):
        prompt_to_send = build_web_search_prompt(user_msg)
    elif model_lower in ("gemini-research", "deep-research", "research"):
        # Handled specially below for non-stream when deep_research API is available
        prompt_to_send = build_web_search_prompt(user_msg)
    elif model_lower in ("gemini-3-flash", "gemini-flash", "flash"):
        force_gemini_model = Model.BASIC_FLASH
        # Prefer text answers for normal chat (still allow images if user explicitly asks)
        if system_content is None:
            prompt_to_send = (
                "You are a helpful assistant. Reply with text. "
                "Only generate an image if the user explicitly asks to draw/generate an image.\n\n"
                f"User: {user_msg}"
            )
    elif model_lower in ("gemini-3-pro", "gemini-pro", "pro"):
        force_gemini_model = Model.BASIC_PRO
        if system_content is None:
            prompt_to_send = (
                "You are a helpful assistant. Reply with text. "
                "Only generate an image if the user explicitly asks to draw/generate an image.\n\n"
                f"User: {user_msg}"
            )
    elif model_lower in ("gemini", "gemini-text", "chat", "default", ""):
        # Default chat: text-first — do NOT wrap with build_image_prompt
        force_gemini_model = Model.BASIC_FLASH
        if system_content is None:
            prompt_to_send = (
                "You are a helpful chat assistant. Answer in the user's language. "
                "Use plain text. Do not generate images or videos unless the user "
                "explicitly asks (e.g. 'vẽ', 'tạo ảnh', 'generate image', 'make a video').\n\n"
                f"User: {user_msg}"
            )
        
    # Increment usage counter
    increment_request_count(acc_id)
        
    # SSE Stream response
    if payload.stream:
        async def stream_generator():
            created_time = int(time.time())
            chat_id = f"chatcmpl-{created_time}"
            
            # Initial chunk (role indicator)
            initial_chunk = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": payload.model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None
                    }
                ]
            }
            yield f"data: {json.dumps(initial_chunk)}\n\n"
            
            last_output = None
            completion_chars = 0
            try:
                async for output in cl.generate_content_stream(
                    prompt_to_send,
                    chat=chat_session,
                    gem=gem_obj
                ):
                    last_output = output
                    delta = output.text_delta
                    if delta:
                        completion_chars += len(delta)
                        chunk = {
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": payload.model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": delta},
                                    "finish_reason": None
                                }
                            ]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                        
                # After streaming finishes: save images + videos to /static
                if last_output:
                    try:
                        base_url = str(request.base_url).rstrip("/")
                        media = await extract_and_save_media(last_output, ROOT / "static", base_url)
                        if media.get("pending_downloads"):
                            await enqueue_media_jobs(media["pending_downloads"])
                        md = media.get("markdown_extra") or ""
                        if md:
                            completion_chars += len(md)
                            chunk = {
                                "id": chat_id,
                                "object": "chat.completion.chunk",
                                "created": created_time,
                                "model": payload.model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"content": md},
                                        "finish_reason": None,
                                    }
                                ],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                        if media.get("images"):
                            ops_stats["images_generated"] = int(ops_stats.get("images_generated") or 0) + len(media["images"])
                        if media.get("videos"):
                            ops_stats["videos_generated"] = int(ops_stats.get("videos_generated") or 0) + len(media["videos"])
                    except Exception as media_err:
                        logger.error(f"Failed to extract stream media: {media_err}")
                        
                # Final stop chunk
                stop_chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": payload.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop"
                        }
                    ]
                }
                yield f"data: {json.dumps(stop_chunk)}\n\n"
                
                # Record successful streaming analytics
                completion_tokens = max(1, completion_chars // 4)
                await record_analytics_request("/v1/chat/completions", acc_id, True, prompt_tokens, completion_tokens)
                
                # Update log entry in-place
                log_entry = getattr(request.state, "log_entry", None)
                if log_entry:
                    log_entry["completion_tokens"] = completion_tokens
                    log_entry["tokens"] = prompt_tokens + completion_tokens
                
            except Exception as e:
                logger.error(f"Error in chat completions stream: {e}")
                # Auto cookie recovery + cool-down so next request failovers
                if is_cookie_auth_failure(e) or isinstance(e, TemporarilyBlocked):
                    try:
                        await mark_account_auth_failed(acc_id, e)
                    except Exception as rec_err:
                        logger.warning(f"Cookie recovery trigger failed: {rec_err}")
                    err_msg = (
                        f"Cookie/session issue on provider {acc_id}. "
                        f"Failover/recovery armed — client should retry. ({e})"
                    )
                else:
                    err_msg = f"Error during stream: {e}"
                err_chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": payload.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"\n\n[{err_msg}]"},
                            "finish_reason": "stop"
                        }
                    ]
                }
                yield f"data: {json.dumps(err_chunk)}\n\n"
                # Record failed streaming analytics
                completion_tokens = max(1, completion_chars // 4)
                await record_analytics_request("/v1/chat/completions", acc_id, False, prompt_tokens, completion_tokens)
                
                # Update log entry in-place
                log_entry = getattr(request.state, "log_entry", None)
                if log_entry:
                    log_entry["completion_tokens"] = completion_tokens
                    log_entry["tokens"] = prompt_tokens + completion_tokens
                
            yield "data: [DONE]\n\n"
            
        return StreamingResponse(stream_generator(), media_type="text/event-stream")
        
    # Non-stream response — multi-provider failover for 24/7 resilience
    else:
        tried: set = set()
        last_error: Optional[BaseException] = None
        use_cl = cl
        use_acc = acc_id
        use_chat = chat_session

        for attempt in range(max(1, MAX_FAILOVER_ATTEMPTS)):
            if attempt > 0:
                # Fail over to another healthy provider
                tried.add(use_acc)
                await apply_newer_extension_cookies_if_changed()
                use_cl, use_acc = await get_client_for_session(
                    session_id, exclude=tried, force_rotate=True
                )
                if not use_cl:
                    break
                sessions[session_id] = use_cl.start_chat()
                use_chat = sessions[session_id]
                request.state.provider_id = use_acc
                ops_stats["failover_count"] = int(ops_stats.get("failover_count") or 0) + 1
                logger.warning(
                    f"[24/7] Failover attempt {attempt + 1}/{MAX_FAILOVER_ATTEMPTS} → provider {use_acc}"
                )

            try:
                # Optional deep research path for research models
                response = None
                if model_lower in ("gemini-research", "deep-research", "research") and hasattr(
                    use_cl, "deep_research"
                ):
                    try:
                        dr = await use_cl.deep_research(user_msg, poll_interval=8.0, timeout=480.0)
                        # Build a mock-like object with text/images if possible
                        class _DROut:
                            pass
                        response = _DROut()
                        response.text = getattr(dr, "report", None) or getattr(dr, "text", None) or str(dr)
                        response.images = []
                        response.videos = []
                        response.media = []
                        response.thoughts = None
                        ops_stats["research_runs"] = int(ops_stats.get("research_runs") or 0) + 1
                    except Exception as dr_err:
                        logger.warning(f"deep_research unavailable, fallback generate_content: {dr_err}")
                        response = None

                if response is None:
                    gen_kwargs = {}
                    if force_gemini_model is not None:
                        gen_kwargs["model"] = force_gemini_model
                    response = await use_cl.generate_content(
                        prompt_to_send,
                        chat=use_chat,
                        gem=gem_obj,
                        **gen_kwargs,
                    )

                base_url = str(request.base_url).rstrip("/")
                base_url = str(request.base_url).rstrip("/")
                content = ""
                media = {"images": [], "videos": [], "thoughts": None, "markdown_extra": ""}
                try:
                    content = response.text or ""
                except Exception:
                    content = str(response)
                try:
                    media = await extract_and_save_media(response, ROOT / "static", base_url)
                    if media.get("pending_downloads"):
                        await enqueue_media_jobs(media["pending_downloads"])
                    content = (media.get("text") or content or "") + (media.get("markdown_extra") or "")
                    if media.get("images"):
                        ops_stats["images_generated"] = int(ops_stats.get("images_generated") or 0) + len(media["images"])
                    if media.get("videos"):
                        ops_stats["videos_generated"] = int(ops_stats.get("videos_generated") or 0) + len(media["videos"])
                except Exception as media_err:
                    # Never fail plain text chat because media extraction broke
                    logger.warning(f"media extract skipped: {media_err}")

                created_time = int(time.time())
                completion_tokens = max(1, len(content) // 4)
                request.state.completion_tokens = completion_tokens
                request.state.provider_id = use_acc
                total_tokens = prompt_tokens + completion_tokens
                touch_session(session_id)

                return {
                    "id": f"chatcmpl-{created_time}",
                    "object": "chat.completion",
                    "created": created_time,
                    "model": payload.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                    },
                    "gemini_media": {
                        "images": media.get("images") or [],
                        "videos": media.get("videos") or [],
                        "thoughts": media.get("thoughts"),
                    },
                }
            except Exception as e:
                last_error = e
                logger.error(f"Error in chat completions (provider={use_acc}, attempt={attempt + 1}): {e}")
                if is_cookie_auth_failure(e) or isinstance(e, TemporarilyBlocked):
                    try:
                        await mark_account_auth_failed(use_acc, e)
                    except Exception as rec_err:
                        logger.warning(f"Cookie recovery trigger failed: {rec_err}")
                    # try next provider / recovered extension cookies
                    continue
                # Non-auth errors: don't spin failover forever
                raise HTTPException(status_code=500, detail=str(e))

        # All attempts exhausted
        detail = str(last_error) if last_error else "No active providers"
        if last_error and is_cookie_auth_failure(last_error):
            raise HTTPException(
                status_code=401,
                detail=(
                    f"All providers failed auth/cookies ({detail}). "
                    "Extension was asked for fresh cookies — keep Chrome logged into Gemini and retry shortly."
                ),
            )
        raise HTTPException(
            status_code=503,
            detail=f"All failover attempts failed: {detail}",
        )

# ---------------------------------------------------------------------------
# Multi-modal OpenAI-style endpoints: images / videos / web research
# ---------------------------------------------------------------------------

class ImageGenerationRequest(BaseModel):
    prompt: str
    n: Optional[int] = 1
    size: Optional[str] = "1024x1024"
    response_format: Optional[str] = "url"  # url | b64_json (url only for now)
    model: Optional[str] = "gemini-image"
    user: Optional[str] = None


class VideoGenerationRequest(BaseModel):
    prompt: str
    model: Optional[str] = "gemini-video"
    user: Optional[str] = None


class ResearchRequest(BaseModel):
    query: str
    mode: Optional[str] = "auto"  # auto | quick | deep
    model: Optional[str] = "gemini-research"
    user: Optional[str] = None


class ExtensionMediaUpload(BaseModel):
    job_id: str
    filename: Optional[str] = None
    content_base64: Optional[str] = None
    content_type: Optional[str] = "application/octet-stream"
    error: Optional[str] = None


@app.post("/v1/images/generations", dependencies=[Depends(verify_api_key)])
@app.post("/images/generations", dependencies=[Depends(verify_api_key)])
async def images_generations(payload: ImageGenerationRequest, request: Request):
    """
    Reliable image generation:
      1) Gemini Web + Flash (host cookies)
      2) Official Google AI Studio API keys (providers type=api_key)
    Always saves under /static when successful.
    """
    if not payload.prompt or not payload.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")

    try:
        from media_pipeline import (
            collect_official_keys,
            collect_web_clients,
            reliable_generate_image,
        )
    except ImportError:
        from media_pipeline import (  # type: ignore
            collect_official_keys,
            collect_web_clients,
            reliable_generate_image,
        )

    base_url = str(request.base_url).rstrip("/")
    web_clients = collect_web_clients(
        client_pool, default_client, default_client_status, SECURE_1PSID
    )
    official_keys = collect_official_keys(client_pool, load_accounts())

    if not web_clients and not official_keys:
        await request_fresh_cookies_from_extension("image gen needs provider", ttl=90)
        await apply_newer_extension_cookies_if_changed()
        web_clients = collect_web_clients(
            client_pool, default_client, default_client_status, SECURE_1PSID
        )

    if web_clients:
        increment_request_count(web_clients[0][1])

    result = await reliable_generate_image(
        prompt=payload.prompt,
        root=ROOT,
        base_url=base_url,
        web_clients=web_clients,
        official_api_keys=official_keys,
    )
    if result.get("pending_downloads"):
        await enqueue_media_jobs(result["pending_downloads"])

    if not result.get("ok"):
        raise HTTPException(
            status_code=503,
            detail={
                "message": result.get("error") or "Image generation failed",
                "attempts": result.get("attempts"),
                "hint": (
                    "Run on HOST with start_host_24_7.ps1 (not Docker on Windows), "
                    "Extension port 8000 + gemini.google.com login, "
                    "or add Google AI Studio API key as provider."
                ),
            },
        )

    data = []
    for im in result.get("images") or []:
        if im.get("url"):
            data.append(
                {
                    "url": im["url"],
                    "revised_prompt": payload.prompt,
                    "title": im.get("title"),
                    "source_url": im.get("source_url"),
                    "filename": im.get("filename"),
                }
            )
    ops_stats["images_generated"] = int(ops_stats.get("images_generated") or 0) + len(data)
    n = max(1, min(int(payload.n or 1), max(len(data), 1)))
    return {
        "created": int(time.time()),
        "data": data[:n],
        "backend": result.get("backend"),
        "text": result.get("text"),
        "attempts": result.get("attempts"),
    }


@app.post("/v1/videos/generations", dependencies=[Depends(verify_api_key)])
@app.post("/videos/generations", dependencies=[Depends(verify_api_key)])
async def videos_generations(payload: VideoGenerationRequest, request: Request):
    """
    Reliable video generation:
      1) Gemini Web video (if URL available)
      2) Extension download queue for gated URLs
      3) GUARANTEED fallback: generate keyframe image then animate to MP4 (ffmpeg/imageio-ffmpeg)
    """
    if not payload.prompt or not payload.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")

    try:
        from media_pipeline import (
            collect_official_keys,
            collect_web_clients,
            reliable_generate_video,
        )
    except ImportError:
        from media_pipeline import (  # type: ignore
            collect_official_keys,
            collect_web_clients,
            reliable_generate_video,
        )

    base_url = str(request.base_url).rstrip("/")
    web_clients = collect_web_clients(
        client_pool, default_client, default_client_status, SECURE_1PSID
    )
    official_keys = collect_official_keys(client_pool, load_accounts())

    if not web_clients and not official_keys:
        await request_fresh_cookies_from_extension("video gen needs provider", ttl=90)
        await apply_newer_extension_cookies_if_changed()
        web_clients = collect_web_clients(
            client_pool, default_client, default_client_status, SECURE_1PSID
        )

    if web_clients:
        increment_request_count(web_clients[0][1])

    # Prefer existing cat image if prompt mentions cat and file exists
    prefer = None
    cat = ROOT / "static" / "20260722075105_f29b8edd32_image.png"
    if "cat" in payload.prompt.lower() or "mèo" in payload.prompt.lower() or "meo" in payload.prompt.lower():
        if cat.exists():
            prefer = cat

    result = await reliable_generate_video(
        prompt=payload.prompt,
        root=ROOT,
        base_url=base_url,
        web_clients=web_clients,
        official_api_keys=official_keys,
        prefer_image_path=prefer,
    )
    job_ids = []
    if result.get("pending_downloads"):
        job_ids = await enqueue_media_jobs(result["pending_downloads"])

    if not result.get("ok"):
        raise HTTPException(
            status_code=503,
            detail={
                "message": result.get("error") or "Video generation failed",
                "attempts": result.get("attempts"),
                "keyframe": result.get("keyframe"),
                "hint": "Install imageio-ffmpeg or ffmpeg; run on HOST; keep Extension cookies fresh.",
            },
        )

    videos = result.get("videos") or []
    ops_stats["videos_generated"] = int(ops_stats.get("videos_generated") or 0) + len(videos)
    return {
        "created": int(time.time()),
        "status": result.get("status") or "ok",
        "data": videos,
        "backend": result.get("backend"),
        "keyframe": result.get("keyframe"),
        "extension_jobs": job_ids,
        "text": result.get("text"),
        "message": result.get("message"),
        "attempts": result.get("attempts"),
    }


@app.post("/v1/research", dependencies=[Depends(verify_api_key)])
@app.post("/v1/web_search", dependencies=[Depends(verify_api_key)])
@app.post("/research", dependencies=[Depends(verify_api_key)])
async def web_research(payload: ResearchRequest, request: Request):
    """
    Web lookup / deep research via Gemini.
    mode=quick → search-style generate_content
    mode=deep  → deep_research workflow when available
    mode=auto  → try deep, fallback quick
    """
    if not payload.query or not payload.query.strip():
        raise HTTPException(status_code=400, detail="query is required")

    cl, acc_id = await get_active_gemini_client()
    if not cl:
        raise HTTPException(status_code=503, detail="No active Gemini provider for research")

    increment_request_count(acc_id)
    mode = (payload.mode or "auto").lower()
    base_url = str(request.base_url).rstrip("/")
    used = "quick"
    text = ""
    media = {"images": [], "videos": [], "markdown_extra": ""}

    try:
        if mode in ("deep", "auto") and hasattr(cl, "deep_research"):
            try:
                dr = await cl.deep_research(payload.query, poll_interval=8.0, timeout=600.0)
                text = (
                    getattr(dr, "report", None)
                    or getattr(dr, "text", None)
                    or getattr(dr, "result", None)
                    or str(dr)
                )
                used = "deep"
                ops_stats["research_runs"] = int(ops_stats.get("research_runs") or 0) + 1
            except Exception as e:
                logger.warning(f"deep research failed, quick fallback: {e}")
                if mode == "deep":
                    raise HTTPException(status_code=502, detail=f"Deep research failed: {e}")

        if used != "deep":
            output = await cl.generate_content(build_web_search_prompt(payload.query))
            media = await extract_and_save_media(output, ROOT / "static", base_url)
            if media.get("pending_downloads"):
                await enqueue_media_jobs(media["pending_downloads"])
            text = (media.get("text") or "") + (media.get("markdown_extra") or "")
            used = "quick"
            ops_stats["research_runs"] = int(ops_stats.get("research_runs") or 0) + 1

        return {
            "created": int(time.time()),
            "mode_used": used,
            "query": payload.query,
            "answer": text,
            "images": media.get("images") or [],
            "videos": media.get("videos") or [],
            "provider_id": acc_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        if is_cookie_auth_failure(e):
            await mark_account_auth_failed(acc_id, e)
            raise HTTPException(status_code=401, detail=f"Auth/cookie error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/media/jobs", dependencies=[Depends(verify_api_key)])
async def list_media_jobs():
    async with media_jobs_lock:
        return {"jobs": list(media_jobs.values())}


@app.get("/v1/media/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def get_media_job(job_id: str):
    async with media_jobs_lock:
        job = media_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job


# Extension bridge for media download (no dashboard token)
@app.get("/sync/media-jobs")
async def extension_list_media_jobs(request: Request):
    _mark_extension_heartbeat(request)
    async with media_jobs_lock:
        pending = [j for j in media_jobs.values() if j.get("status") == "pending"]
    return {"jobs": pending[:10]}


@app.post("/sync/media-upload")
async def extension_media_upload(payload: ExtensionMediaUpload, request: Request):
    """
    Extension uploads a file it downloaded with browser cookies (base64).
    """
    import base64

    _mark_extension_heartbeat(request)
    async with media_jobs_lock:
        job = media_jobs.get(payload.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")

        if payload.error:
            job["status"] = "error"
            job["error"] = payload.error
            return {"status": "error_recorded"}

        if not payload.content_base64:
            raise HTTPException(status_code=400, detail="content_base64 required")

        try:
            raw = base64.b64decode(payload.content_base64)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid base64: {e}")

        static_dir = ROOT / "static"
        static_dir.mkdir(parents=True, exist_ok=True)
        ext = ".mp4" if job.get("media_type") == "video" else ".png"
        if payload.content_type:
            if "png" in payload.content_type:
                ext = ".png"
            elif "jpeg" in payload.content_type or "jpg" in payload.content_type:
                ext = ".jpg"
            elif "webp" in payload.content_type:
                ext = ".webp"
            elif "webm" in payload.content_type:
                ext = ".webm"
            elif "mp4" in payload.content_type:
                ext = ".mp4"
        fn = payload.filename or f"ext_{payload.job_id}{ext}"
        # sanitize filename
        fn = Path(fn).name
        dest = static_dir / fn
        dest.write_bytes(raw)
        job["status"] = "done"
        job["filename"] = fn
        job["public_url"] = f"/static/{fn}"
        job["error"] = None
        ops_stats["media_jobs_completed"] = int(ops_stats.get("media_jobs_completed") or 0) + 1
        logger.info(f"Extension media upload saved: {fn} ({len(raw)} bytes)")
        return {"status": "ok", "filename": fn, "url": job["public_url"]}


# Dashboard Authentication Settings
DASHBOARD_SESSION_TOKEN = secrets.token_hex(16)

class LoginPayload(BaseModel):
    password: str

@app.post("/api/login")
async def api_login(payload: LoginPayload):
    config = load_config()
    expected_password = config.get("dashboard_password", os.getenv("DASHBOARD_PASSWORD", "123456"))
    if payload.password == expected_password:
        return {"status": "success", "token": DASHBOARD_SESSION_TOKEN}
    else:
        raise HTTPException(status_code=401, detail="Incorrect password")

class ChangePasswordPayload(BaseModel):
    current_password: str
    new_password: str

@app.post("/api/change-password")
async def change_password(payload: ChangePasswordPayload):
    config = load_config()
    current_stored = config.get("dashboard_password", os.getenv("DASHBOARD_PASSWORD", "123456"))
    if payload.current_password != current_stored:
        raise HTTPException(status_code=400, detail="Mật khẩu hiện tại không chính xác")
    
    if len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="Mật khẩu mới phải từ 6 ký tự trở lên")
        
    config["dashboard_password"] = payload.new_password
    save_config(config)
    
    # Generate new token so all existing sessions/browsers are logged out!
    global DASHBOARD_SESSION_TOKEN
    DASHBOARD_SESSION_TOKEN = secrets.token_hex(16)
    
    return {"status": "success", "message": "Đổi mật khẩu thành công. Vui lòng đăng nhập lại."}

# Middleware to protect all /api/ endpoints (excluding /api/login)
@app.middleware("http")
async def auth_dashboard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path != "/api/login":
        token = request.headers.get("X-Dashboard-Token")
        if not token or token != DASHBOARD_SESSION_TOKEN:
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized. Invalid dashboard session token."}
            )
    response = await call_next(request)
    return response

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Return JSON errors so the playground never shows opaque {_raw: Internal Server Error}."""
    logger.exception(f"Unhandled error on {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "detail": str(exc) or exc.__class__.__name__,
            "path": request.url.path,
            "hint": (
                "If image/video fails: run API on HOST (start_host_24_7.ps1), "
                "not Docker on Windows. Keep Extension cookies fresh."
            ),
        },
    )


if __name__ == "__main__":
    import uvicorn
    # Reduce access_log noise (Extension polls every few seconds)
    access_log = os.getenv("ACCESS_LOG", "0") == "1"
    # Single worker required: in-memory pool/sessions are not multi-process safe
    uvicorn.run(
        "api_server:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
        workers=1,
        log_level=os.getenv("LOG_LEVEL", "info"),
        access_log=access_log,
        timeout_keep_alive=75,
    )
