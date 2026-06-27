import asyncio
import sys
from runtime.session import VoiceSession

async def main():
    session = VoiceSession()
    try:
        await session.run()
    except KeyboardInterrupt:
        print("\nExiting...", flush=True)
    except Exception as e:
        print(f"\nFatal error in application: {e}", flush=True)
        sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...", flush=True)
