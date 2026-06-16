import asyncio
import sys
import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

# Set UTF-8 output encoding for Windows command line
sys.stdout.reconfigure(encoding='utf-8')

# Ensure the package src directory is in the path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from gemini_webapi import GeminiClient, logger
from gemini_webapi.types.image import WebImage, GeneratedImage

app = FastAPI(title="Gemini WebAPI Diagnostic Dashboard")

# Enable CORS for local testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# User credentials provided in previous turn
SECURE_1PSID = os.getenv("SECURE_1PSID", "g.a000_AjYuI_xzJbWKdaQ-Vh1O4BvZqNjq5NuDjfrrE2U8AKnAjtzZcyQ8Y1S3PQx26KDRPiWjAACgYKAZ8SARYSFQHGX2MiWbE39ZNBHCahqqwmum1h8hoVAUF8yKp70iSvj-LLuwyTioABObAe0076")
SECURE_1PSIDTS = os.getenv("SECURE_1PSIDTS", "sidts-CjYByojQUx_NAcNLc5_XGJdNzcO5e54xsvLiMGb81JlyQpsnCzc3dpBERsT-JebpUzxLs9GeyaAQAA")

# Global client and initialization status
client: Optional[GeminiClient] = None
is_connected = False
connection_error: Optional[str] = None
deep_research_feature_present = False
active_chat = None

class ChatPayload(BaseModel):
    message: str
    gem_id: Optional[str] = None

class OpenAIMessage(BaseModel):
    role: str
    content: str

class OpenAIRequest(BaseModel):
    model: Optional[str] = "gemini"
    messages: list[OpenAIMessage]
    stream: Optional[bool] = False

def mask_cookie(value: str) -> str:
    if not value or len(value) < 15:
        return "Invalid"
    return f"{value[:6]}...{value[-6:]}"

@app.on_event("startup")
async def startup_event():
    global client, is_connected, connection_error, deep_research_feature_present
    logger.info("Initializing GeminiClient with provided cookies...")
    client = GeminiClient(SECURE_1PSID, SECURE_1PSIDTS, verify=False)
    
    try:
        await client.init(timeout=45, auto_close=False, auto_refresh=True)
        is_connected = True
        logger.info("GeminiClient initialized successfully!")
        
        # Check deep research availability
        try:
            status = await client.inspect_account_status()
            deep_research_feature_present = status.get("summary", {}).get("deep_research_feature_present", False)
            logger.info(f"Deep Research feature present: {deep_research_feature_present}")
        except Exception as e:
            logger.warning(f"Failed to check deep research status: {e}")
            
        # Prefetch gems on startup
        try:
            await client.fetch_gems(include_hidden=False)
            logger.info("Gems prefetched successfully.")
        except Exception as e:
            logger.warning(f"Failed to prefetch Gems on startup: {e}")
            
    except Exception as e:
        is_connected = False
        connection_error = str(e)
        logger.error(f"Failed to initialize GeminiClient: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    global client
    if client:
        await client.close()
        logger.info("GeminiClient closed.")

@app.get("/")
async def get_dashboard():
    dashboard_path = ROOT / "dashboard.html"
    if not dashboard_path.exists():
        raise HTTPException(status_code=404, detail="dashboard.html not found.")
    return FileResponse(str(dashboard_path))

@app.get("/api/status")
async def get_status():
    global client, is_connected, connection_error, deep_research_feature_present
    
    psid_masked = mask_cookie(SECURE_1PSID)
    psidts_masked = mask_cookie(SECURE_1PSIDTS)
    
    if is_connected and client:
        return {
            "connected": True,
            "psid": psid_masked,
            "psidts": psidts_masked,
            "access_token": mask_cookie(client.access_token) if client.access_token else "None",
            "session_id": client.session_id or "None",
            "build_label": client.build_label or "None",
            "deep_research_feature_present": deep_research_feature_present
        }
    else:
        return {
            "connected": False,
            "psid": psid_masked,
            "psidts": psidts_masked,
            "error": connection_error or "Not initialized",
            "deep_research_feature_present": False
        }
async def get_client_gems():
    global client
    try:
        return client.gems
    except RuntimeError:
        logger.info("Gems cache is empty. Fetching gems from server...")
        await client.fetch_gems(include_hidden=False)
        return client.gems

@app.get("/api/chats")
async def get_chats():
    global client, is_connected
    if not is_connected or not client:
        raise HTTPException(status_code=400, detail="Client is not connected.")
    try:
        chats = client.list_chats() or []
        # Return format: [{"cid": "...", "title": "..."}]
        return [{"cid": c.cid, "title": c.title} for c in chats]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch chats: {e}")

@app.get("/api/gems")
async def get_gems():
    global client, is_connected
    if not is_connected or not client:
        raise HTTPException(status_code=400, detail="Client is not connected.")
    try:
        gems = await get_client_gems()
        gems_list = []
        for gem in gems:
            gems_list.append({
                "id": gem.id,
                "name": gem.name,
                "description": gem.description,
                "predefined": gem.predefined
            })
        return gems_list
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch gems: {e}")

@app.post("/api/chat/new")
async def start_new_chat():
    global client, active_chat
    if not client:
        raise HTTPException(status_code=400, detail="Client not initialized.")
    active_chat = client.start_chat()
    logger.info("New ChatSession started.")
    return {"status": "success"}

@app.post("/api/chat/send")
async def send_message(payload: ChatPayload):
    global client, is_connected, active_chat
    if not is_connected or not client:
        raise HTTPException(status_code=400, detail="Client is not connected.")
    
    if active_chat is None:
        active_chat = client.start_chat()
        logger.info("Auto-started a new ChatSession.")
    
    try:
        gem_obj = None
        if payload.gem_id and payload.gem_id != "null" and payload.gem_id != "":
            # Look up the gem from client gems
            gems = await get_client_gems()
            for gem in gems:
                if gem.id == payload.gem_id:
                    gem_obj = gem
                    break
        
        response = await client.generate_content(
            prompt=payload.message,
            gem=gem_obj,
            chat=active_chat
        )
        
        # Parse images if any
        images = []
        if response.images:
            for img in response.images:
                if isinstance(img, WebImage):
                    images.append({"url": img.url, "title": img.title})
                elif isinstance(img, GeneratedImage):
                    images.append({"url": img.url, "title": "Generated image"})

        return {
            "text": response.text or "",
            "thoughts": response.thoughts or "",
            "images": images
        }
    except Exception as e:
        logger.error(f"Error generating content: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/chat/completions")
async def openai_chat_completions(payload: OpenAIRequest):
    global client, is_connected
    if not is_connected or not client:
        raise HTTPException(status_code=400, detail="Client is not connected.")
    
    # Get the last user message
    user_msg = ""
    for msg in reversed(payload.messages):
        if msg.role == "user":
            user_msg = msg.content
            break
            
    if not user_msg:
        raise HTTPException(status_code=400, detail="No user message found.")
        
    try:
        import time
        response = await client.generate_content(prompt=user_msg)
        
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.model or "gemini",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response.text or ""
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }
    except Exception as e:
        logger.error(f"OpenAI compatibility error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard_server:app", host="0.0.0.0", port=8000, reload=False)
