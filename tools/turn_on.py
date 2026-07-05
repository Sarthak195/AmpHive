import asyncio
import os
import sys
from tapo import ApiClient

async def main():
    # Tapo App Credentials — from environment, never hardcoded.
    tapo_email = os.environ.get("TAPO_EMAIL", "")
    tapo_password = os.environ.get("TAPO_PASSWORD", "")
    plug_ip = os.environ.get("TAPO_PLUG_IP", "192.168.1.6")
    if not tapo_email or not tapo_password:
        sys.exit("Set TAPO_EMAIL and TAPO_PASSWORD environment variables before running.")


    print(f"Connecting to plug at {plug_ip}...")
    try:
        client = ApiClient(tapo_email, tapo_password)
        device = await client.p110(plug_ip)
        
        print("Turning ON the plug...")
        await device.on()
        print("Success! The plug is now ON.")
        
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
