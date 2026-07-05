import asyncio
import os
import sys
from tapo import ApiClient

async def main():
    # Tapo App Credentials — from environment, never hardcoded.
    tapo_email = os.environ.get("TAPO_EMAIL", "")
    tapo_password = os.environ.get("TAPO_PASSWORD", "")
    if not tapo_email or not tapo_password:
        sys.exit("Set TAPO_EMAIL and TAPO_PASSWORD environment variables before running.")

    # Plug IP on your local network (Tapo App: Device Settings -> Device Info).
    plug_ip = os.environ.get("TAPO_PLUG_IP", "192.168.1.4")

    print(f"Authenticating with Tapo account: {tapo_email}")
    client = ApiClient(tapo_email, tapo_password)
    
    print(f"Connecting to local plug at {plug_ip}...")
    try:
        # Initialize the connection to the P110 plug
        device = await client.p110(plug_ip)
        
        # Test 1: Get Info
        print("Fetching device info...")
        info = await device.get_device_info()
        info_dict = info.to_dict()
        print(f"Connected successfully to: {info_dict.get('nickname')} (Model: {info_dict.get('model')})")
        print(f"   Current State: {'ON' if info_dict.get('device_on') else 'OFF'}")
        
        # Test 2: Turn ON
        print("\nTurning ON the plug...")
        await device.on()
        
        print("Waiting 3 seconds...")
        await asyncio.sleep(3)
        
        # Test 3: Turn OFF
        print("Turning OFF the plug...")
        await device.off()
        
        print("\nTest completed successfully!")
        
    except Exception as e:
        print(f"\nError connecting to the plug: {e}")
        print("\nTroubleshooting Tips:")
        print("1. Check if the IP address is correct in the Tapo App.")
        print("2. Make sure your PC is on the same Wi-Fi network as the plug.")
        print("3. Ensure 'Third-Party Compatibility' is enabled in the Tapo app (Device Settings -> Advanced -> Third-Party Compatibility).")

if __name__ == "__main__":
    asyncio.run(main())
