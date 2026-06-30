import os
import sys
import requests
from dotenv import load_dotenv

# Load .env file
load_dotenv()

ACCOUNT_SID = os.getenv("EXOTEL_ACCOUNT_SID")
API_KEY = os.getenv("EXOTEL_API_KEY")
API_TOKEN = os.getenv("EXOTEL_API_TOKEN")
SUBDOMAIN = os.getenv("EXOTEL_SUBDOMAIN", "api.exotel.com")
VIRTUAL_NUMBER = os.getenv("EXOTEL_VIRTUAL_NUMBER")

def main():
    if not all([ACCOUNT_SID, API_KEY, API_TOKEN]):
        print("Error: Please make sure EXOTEL_ACCOUNT_SID, EXOTEL_API_KEY, and EXOTEL_API_TOKEN are set in .env")
        sys.exit(1)

    print("\n==========================================")
    print("      Exotel Outbound Call Initiator      ")
    print("==========================================\n")
    
    # 1. Get phone number to call
    user_num = input("1. Enter the phone number to call (e.g. +91XXXXXXXXXX): ").strip()
    if not user_num:
        print("Error: Destination phone number is required.")
        sys.exit(1)
        
    # 2. Get Exophone virtual number
    exophone = VIRTUAL_NUMBER
    if not exophone or exophone == "your_exophone_here":
        exophone = input("2. Enter your Exotel Virtual Number (Exophone): ").strip()
    else:
        confirm = input(f"2. Use saved Exophone {exophone}? (Y/n): ").strip().lower()
        if confirm == 'n':
            exophone = input("   Enter your Exotel Virtual Number (Exophone): ").strip()
            
    if not exophone:
        print("Error: Exophone (virtual number) is required.")
        sys.exit(1)
        
    # 3. Get the StreamUrl
    stream_url = input("3. Enter the Webhook URL (e.g. wss://xxxx.ngrok-free.dev/media): ").strip()
    if not stream_url:
        print("Error: Stream Webhook URL is required.")
        sys.exit(1)
        
    # Append ?sample-rate=16000 if no query parameters exist to enable wideband 16kHz HD Voice
    if "?" not in stream_url:
        stream_url += "?sample-rate=16000"
        print(f"   [HD Voice] Appended '?sample-rate=16000' to Webhook URL for superior audio quality.")
        
    # Build API URL
    api_url = f"https://{SUBDOMAIN}/v1/Accounts/{ACCOUNT_SID}/Calls/connect.json"
    
    payload = {
        "From": user_num,
        "CallerId": exophone,
        "StreamUrl": stream_url,
        "StreamType": "bidirectional"
    }
    
    print(f"\nTriggering outbound call from Exotel...")
    try:
        response = requests.post(
            api_url,
            auth=(API_KEY, API_TOKEN),
            data=payload,
            timeout=15
        )
        print(f"\nExotel API Response [Status {response.status_code}]:")
        try:
            print(json.dumps(response.json(), indent=2))
        except Exception:
            print(response.text)
    except Exception as e:
        print(f"Error making API request: {e}")

if __name__ == "__main__":
    # Import json safely inside main block
    import json
    main()
