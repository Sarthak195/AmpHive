from contextlib import asynccontextmanager
import os
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from typing import List

from backend.services.mqtt_manager import MQTTManager

# Fetch environment variables for broker configuration
MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "localhost")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", 1883))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", None)
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", None)

# Setup lifespan events for FastAPI
mqtt_manager = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global mqtt_manager
    # Initialize and start the MQTT connection
    mqtt_manager = MQTTManager(
        broker_host=MQTT_BROKER_HOST,
        broker_port=MQTT_BROKER_PORT,
        username=MQTT_USERNAME,
        password=MQTT_PASSWORD
    )
    mqtt_manager.start()
    yield
    # Stop the MQTT connection during shutdown
    if mqtt_manager:
        mqtt_manager.stop()

app = FastAPI(
    title="AmpHive Shared EV Charging API",
    description="Backend PaaS control layer orchestrating ESP32 gateways, smart plugs, and headscale security policies.",
    version="1.0.0",
    lifespan=lifespan
)

# --- Pydantic Schemas for Requests and Responses ---

class SessionStartRequest(BaseModel):
    gateway_id: str
    plug_id: int
    user_id: int
    max_duration_seconds: int = 14400 # 4 hours default
    max_kwh: float = 30.0             # 30 kWh limit default

class SessionStopRequest(BaseModel):
    gateway_id: str
    plug_id: int
    session_id: int

class GatewayRegisterRequest(BaseModel):
    gateway_id: str # MAC/UUID
    name: str
    vpn_ip: str

class PlugRegisterRequest(BaseModel):
    gateway_id: str
    name: str
    local_ip: str
    plug_model: str = "tapo_p110"

# --- Endpoints ---

@app.get("/api/health")
def health_check():
    return {"status": "healthy", "service": "amphive-backend"}

@app.post("/api/gateways/register")
def register_gateway(req: GatewayRegisterRequest):
    # In a fully connected database setup, this registers a new ESP32 gateway
    return {
        "status": "registered",
        "gateway_id": req.gateway_id,
        "name": req.name,
        "vpn_ip": req.vpn_ip
    }

@app.post("/api/plugs/register")
def register_plug(req: PlugRegisterRequest):
    # Registers a new 3rd-party smart plug on a specific gateway's local VLAN subnet
    return {
        "status": "registered",
        "gateway_id": req.gateway_id,
        "name": req.name,
        "local_ip": req.local_ip,
        "plug_model": req.plug_model
    }

@app.post("/api/sessions/start")
def start_charging_session(req: SessionStartRequest):
    # 1. Check user wallet balances (In-memory mock or DB check)
    # 2. Issue the MQTT command to the ESP32 gateway
    success = mqtt_manager.send_plug_command(
        gateway_id=req.gateway_id,
        plug_id=req.plug_id,
        action="ON",
        max_duration=req.max_duration_seconds,
        max_kwh=req.max_kwh
    )
    
    if not success:
        raise HTTPException(
            status_code=500,
            detail="Failed to publish start command to the designated gateway."
        )
        
    return {
        "status": "starting",
        "gateway_id": req.gateway_id,
        "plug_id": req.plug_id,
        "message": f"Sent ON command to plug {req.plug_id} on gateway {req.gateway_id}."
    }

@app.post("/api/sessions/stop")
def stop_charging_session(req: SessionStopRequest):
    # 1. Issue the MQTT command to turn the plug off
    success = mqtt_manager.send_plug_command(
        gateway_id=req.gateway_id,
        plug_id=req.plug_id,
        action="OFF"
    )
    
    if not success:
        raise HTTPException(
            status_code=500,
            detail="Failed to publish stop command to the designated gateway."
        )
        
    return {
        "status": "stopping",
        "gateway_id": req.gateway_id,
        "plug_id": req.plug_id,
        "message": f"Sent OFF command to plug {req.plug_id} on gateway {req.gateway_id}."
    }
