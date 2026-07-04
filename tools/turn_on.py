import asyncio
import sys
from tapo import ApiClient

async def main():
    # Tapo App Credentials
    tapo_email = "sjgotnfts1@gmail.com"
    tapo_password = "Nitya@2001"
    plug_ip = "192.168.1.6"
    
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
