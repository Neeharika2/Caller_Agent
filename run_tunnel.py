import os
import sys
import time
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# Import config from workspace
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config

def main():
    if not config.NGROK_AUTHTOKEN or config.NGROK_AUTHTOKEN == "your_ngrok_authtoken_here":
        print("Error: NGROK_AUTHTOKEN is not set in your .env file.")
        print("Please sign up at https://dashboard.ngrok.com and add your authtoken to .env")
        sys.exit(1)

    try:
        import ngrok
        print("[ngrok] Connecting tunnel to port 8000 in a separate process...", flush=True)
        listener = ngrok.forward(addr=8000, authtoken=config.NGROK_AUTHTOKEN)
        public_url = listener.url()
        ws_url = public_url.replace("https://", "wss://").replace("http://", "ws://") + "/media"
        print("\n" + "="*80)
        print(f" ngrok public HTTP URL: {public_url}")
        print(f" Exotel AgentStream Webhook URL: {ws_url}")
        print("="*80 + "\n", flush=True)
        
        # Keep the process alive to hold the tunnel open
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\n[ngrok] Stopping tunnel...", flush=True)
    except Exception as e:
        print(f"[ngrok] Error starting tunnel: {e}", flush=True)

if __name__ == "__main__":
    main()
