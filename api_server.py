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
from gemini_webapi.types.image import WebImage, GeneratedImage
from gemini_webapi.types.gem import Gem

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

# Default Fallback Credentials
SECURE_1PSID = os.getenv("SECURE_1PSID", "g.a000_AjYuI_xzJbWKdaQ-Vh1O4BvZqNjq5NuDjfrrE2U8AKnAjtzZcyQ8Y1S3PQx26KDRPiWjAACgYKAZ8SARYSFQHGX2MiWbE39ZNBHCahqqwmum1h8hoVAUF8yKp70iSvj-LLuwyTioABObAe0076")
SECURE_1PSIDTS = os.getenv("SECURE_1PSIDTS", "sidts-CjYByojQUx_NAcNLc5_XGJdNzcO5e54xsvLiMGb81JlyQpsnCzc3dpBERsT-JebpUzxLs9GeyaAQAA")
API_KEY = os.getenv("API_KEY", "")

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
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = "gemini"
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    max_tokens: Optional[int] = None
    user: Optional[str] = None

class ImageGenerationRequest(BaseModel):
    prompt: str
    n: Optional[int] = 1
    size: Optional[str] = "1024x1024"
    response_format: Optional[str] = "url"
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

class CreateAgentPayload(BaseModel):
    name: str
    description: Optional[str] = ""
    system_prompt: str
    base_model: Optional[str] = "gemini"
    temperature: Optional[float] = 1.0

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
    
    if disable_default:
        logger.info("Fallback default GeminiClient is disabled via dashboard config.")
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
                "error": None
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
                    "error": None
                }
                logger.info(f"GeminiClient for {acc['name']} connected!")
            except Exception as e:
                client_pool[acc_id] = {
                    "client": None,
                    "name": acc["name"],
                    "status": "Disconnected",
                    "requests_count": acc.get("requests_count", 0),
                    "error": str(e)
                }
                logger.error(f"Failed to connect client for {acc['name']}: {e}")

# Client selector rotation logic with sticky session capability
async def get_client_for_session(session_id: str) -> tuple[GeminiClient, str]:
    global rotation_index, default_client_status
    async with pool_lock:
        # 1. Sticky Session Lookup
        acc_id = session_to_account.get(session_id)
        if acc_id:
            if acc_id == "default":
                config = load_config()
                disable_default = config.get("disable_default_provider", False)
                if not disable_default and default_client_status == "Active":
                    return default_client, "default"
            elif acc_id in client_pool and client_pool[acc_id]["status"] == "Active":
                return client_pool[acc_id]["client"], acc_id
                
        # 2. Select next active client in the pool (Round-Robin)
        active_ids = [aid for aid, info in client_pool.items() if info["status"] == "Active"]
        
        if not active_ids:
            config = load_config()
            disable_default = config.get("disable_default_provider", False)
            if disable_default or not default_client:
                return None, "default"
            # Fallback to the default client
            session_to_account[session_id] = "default"
            if default_client_status != "Active" and default_client:
                try:
                    await default_client.init(timeout=45, auto_close=False, auto_refresh=True)
                    default_client_status = "Active"
                except Exception:
                    pass
            return default_client, "default"
            
        selected_id = active_ids[rotation_index % len(active_ids)]
        rotation_index += 1
        session_to_account[session_id] = selected_id
        return client_pool[selected_id]["client"], selected_id

# Helper to save image locally
async def save_image_locally(img) -> str:
    static_path = ROOT / "static"
    static_path.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if isinstance(img, GeneratedImage):
        kwargs["full_size"] = True
    abs_path_str = await img.save(path=str(static_path), verbose=True, **kwargs)
    return Path(abs_path_str).name

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

@app.on_event("startup")
async def startup_event():
    logger.info("Initializing Gemini account connection pool...")
    await initialize_client_pool()

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
        "total_providers_count": total_providers
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
            "error": pool_info.get("error")
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
    global default_client_status
    active_pool_clients = any(info.get("status") == "Active" for info in client_pool.values())
    if default_client_status == "Active" or active_pool_clients:
        return {"status": "healthy", "detail": "Connected to Gemini client pool"}
    else:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "detail": "All Gemini client providers are offline"}
        )

@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models(request: Request):
    is_general_key = getattr(request.state, "is_general_key", True)
    authenticated_agent_id = getattr(request.state, "authenticated_agent_id", None)
    
    models = []
    
    if is_general_key:
        models.append({
            "id": "gemini",
            "object": "model",
            "created": 1686935002,
            "owned_by": "google",
            "description": "Standard Google Gemini Model"
        })
        
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
async def chat_completions(payload: ChatCompletionRequest, request: Request):
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
                
    # Select client
    cl, acc_id = await get_client_for_session(session_id)
    if not cl:
         raise HTTPException(status_code=503, detail="No active Gemini provider accounts are available.")
         
    # Set request state for telemetry
    request.state.provider_id = acc_id
    prompt_tokens = max(1, sum(len(m.content) for m in payload.messages) // 4)
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
        system_content = payload.messages[0].content
        
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
            user_msg = msg.content
            break
            
    if not user_msg:
        raise HTTPException(status_code=400, detail="No user message found in the payload.")
        
    # Handle system message prepending
    prompt_to_send = user_msg
    if system_content:
        prompt_to_send = f"{system_content}\n\n{user_msg}"
        
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
                        
                # After streaming finishes, check for generated/retrieved images
                if last_output and last_output.images:
                    image_markdowns = []
                    for img in last_output.images:
                        try:
                            fn = await save_image_locally(img)
                            base_url = str(request.base_url).rstrip('/')
                            local_url = f"{base_url}/static/{fn}"
                            image_markdowns.append(f"\n\n![Generated Image]({local_url})")
                        except Exception as img_err:
                            logger.error(f"Failed to save image in stream: {img_err}")
                            image_markdowns.append(f"\n\n![Image]({img.url})")
                            
                    if image_markdowns:
                        img_delta = "".join(image_markdowns)
                        completion_chars += len(img_delta)
                        chunk = {
                            "id": chat_id,
                            "object": "chat.completion.chunk",
                            "created": created_time,
                            "model": payload.model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": img_delta},
                                    "finish_reason": None
                                }
                            ]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                        
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
                err_chunk = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": payload.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"\n\n[Error during stream: {e}]"},
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
        
    # Non-stream response
    else:
        try:
            response = await cl.generate_content(
                prompt_to_send,
                chat=chat_session,
                gem=gem_obj
            )
            
            content = response.text or ""
            
            # Check for images and append markdown links
            if response.images:
                image_markdowns = []
                for img in response.images:
                    try:
                        fn = await save_image_locally(img)
                        base_url = str(request.base_url).rstrip('/')
                        local_url = f"{base_url}/static/{fn}"
                        image_markdowns.append(f"\n\n![Generated Image]({local_url})")
                    except Exception as img_err:
                        logger.error(f"Failed to save image: {img_err}")
                        image_markdowns.append(f"\n\n![Image]({img.url})")
                content += "".join(image_markdowns)
                
            created_time = int(time.time())
            completion_tokens = max(1, len(content) // 4)
            request.state.completion_tokens = completion_tokens
            total_tokens = prompt_tokens + completion_tokens
            
            return {
                "id": f"chatcmpl-{created_time}",
                "object": "chat.completion",
                "created": created_time,
                "model": payload.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": content
                        },
                        "finish_reason": "stop"
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens
                }
            }
        except Exception as e:
            logger.error(f"Error in chat completions: {e}")
            raise HTTPException(status_code=500, detail=str(e))

# Image and Video generation features removed

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

if __name__ == "__main__":
    import uvicorn
    # Exposing on 0.0.0.0:8000 for local network integration
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=False)
