# AI Caller Agent

An interactive, real-time voice agent powered by the **Gemini Live API**. This project supports two modes of operation:
1. **Local Voice Session**: Direct real-time interactive voice conversation using your PC's microphone and speakers.
2. **Exotel Telephony Bridge**: A full VoIP/telephony bridge that connects Exotel voice calls directly to Gemini, complete with low-latency audio resampling, voice activity detection (VAD), and barge-in (interruption) handling.

---

## Architecture Overview

```mermaid
graph TD
    subgraph Local Client Mode (app.py)
        A[MicrophoneInput] -->|16kHz PCM| B(VoiceSession)
        B -->|Gemini Live WS| C[Gemini Live API]
        C -->|24kHz PCM| B
        B -->|Speaker Playback| D[SpeakerOutput]
    end

    subgraph Telephony Mode (exotel_server.py)
        E[Phone Caller] <-->|PSTN Call| F[Exotel Telephony Platform]
        F <-->|WS stream /media| G[FastAPI Server: exotel_server.py]
        H[run_tunnel.py: ngrok] <-->|Expose Port 8000| G
        G <-->|Resampled WS Stream| C
        I[make_call.py] -->|Trigger Outbound Call| F
    end
```

---

## Project Structure

*   `app.py`: Entry point for the local interactive voice session.
*   `exotel_server.py`: FastAPI WebSocket server bridging audio stream between Exotel and Gemini Live.
*   `run_tunnel.py`: Automates the ngrok tunnel connection to expose the local server on port 8000.
*   `make_call.py`: Command-line tool to trigger an outbound Exotel call routed to the webhook stream.
*   `config.py`: Centralized configuration loading variables from `.env` and setting audio sample rates.
*   `providers/gemini_live.py`: WebSocket client provider wrapper for connecting and talking to the Gemini Live API.
*   `runtime/session.py`: Coordinates microphone input, speaker output, and the Gemini provider for the local client.
*   `runtime/tools.py`: Generic Gemini Live function-calling registry with a starter `get_current_time` tool.
*   `knowledge/`: Local files searched by the `search_knowledge` tool.
*   `audio/`:
    *   `microphone.py`: Non-blocking microphone capture (using PyAudio) running on standard 16kHz mono.
    *   `speaker.py`: Low-latency thread-safe audio playback stream (using PyAudio) with clear queue functionality.

---

## Setup & Installation

### Prerequisites
*   **Python 3.10 to 3.12**: This project utilizes Python's built-in `audioop` module (for audio resampling). Note that `audioop` is deprecated starting in Python 3.13.
*   **System Audio Drivers**: PyAudio requires PortAudio installed on your system:
    *   *Windows*: Usually pre-compiled wheel files are installed automatically.
    *   *macOS*: Install via Homebrew: `brew install portaudio` and then `pip install pyaudio`.
    *   *Linux (Ubuntu/Debian)*: `sudo apt-get install python3-pyaudio portaudio19-dev`.

### 1. Clone the Repository & Install Dependencies
Create a virtual environment and install the required Python packages:

```bash
# Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Create a `.env` file in the root of the project with the following contents:

```env
# Gemini Configuration
GEMINI_API_KEY=your_gemini_api_key_here
MODEL=gemini-3.1-flash-live-preview

# Exotel Telephony Configuration
EXOTEL_ACCOUNT_SID=your_exotel_account_sid_here
EXOTEL_API_KEY=your_exotel_api_key_here
EXOTEL_API_TOKEN=your_exotel_api_token_here
EXOTEL_SUBDOMAIN=api.exotel.com
EXOTEL_VIRTUAL_NUMBER=your_exotel_virtual_number_here

# Tunnel Configuration
NGROK_AUTHTOKEN=your_ngrok_authtoken_here
```

---

## How to Run

### Workflow 1: Local Voice Session (PC Mic & Speaker)
Run the local voice application:
```bash
python app.py
```
*   The application connects to Gemini Live, sets up your audio hardware, and goes active.
*   Start speaking! You will see live transcriptions of your speech and the model's voice responses printed in the terminal.
*   To exit, press `Ctrl+C`.

---

### Workflow 2: Exotel Telephony Voicebot
To connect a phone call to Gemini, you need to spin up the local server, expose it via ngrok, and route an Exotel call to it.

#### Step 1: Start the FastAPI Media Server
Launch the server which runs on port 8000:
```bash
python exotel_server.py
```

#### Step 2: Open the ngrok Tunnel
In a new terminal window, start the tunnel to generate a public WebSocket URL:
```bash
python run_tunnel.py
```
This will print a public URL resembling:
`wss://<subdomain>.ngrok-free.dev/media?sample-rate=16000`

#### Step 3: Trigger the Outbound Call
In another terminal, run the initiator script:
```bash
python make_call.py
```
You will be prompted to:
1.  **Enter the phone number to call**: Your mobile phone number (e.g. `+91XXXXXXXXXX`).
2.  **Confirm or enter the Exophone virtual number**: Your Exotel phone number.
3.  **Enter the Webhook URL**: Paste the `wss://...` URL printed by `run_tunnel.py` in Step 2.

Exotel will call your phone. As soon as you answer, the greeting prompt will trigger Gemini to say hello, and you can carry on a live bidirectional conversation.

---

## Function Calling Tools

The app now registers tools with Gemini during Live API setup and handles `toolCall` messages manually in both local voice mode and Exotel telephony mode. The first registered tools are:

```python
get_current_time(timezone="Asia/Kolkata")
search_knowledge(query, max_results=3)
```

`search_knowledge` reads local `.md`, `.txt`, and `.json` files from `knowledge/`, scores matching text snippets, and returns the best few results to Gemini. Start by editing `knowledge/faq.md` with your business-specific answers.

To add another tool:

1. Add the Python function in `runtime/tools.py`.
2. Register its Gemini function declaration in `create_default_tool_registry()`.
3. Keep the function return value JSON-serializable, preferably a `dict`.

The bridge will execute matching Python functions and send the result back as a `toolResponse`.

---

## Audio Technical Details

*   **Sample Rate Conversion**:
    *   **Inbound**: Exotel sends audio in 8kHz or 16kHz. The server buffers it and uses `audioop.ratecv` to resample to **16kHz** (Gemini standard).
    *   **Outbound**: Gemini outputs audio at **24kHz**. The server resamples this down to the negotiated Exotel sample rate (e.g., 8kHz/16kHz) dynamically.
*   **Barge-In (Interruption)**:
    *   The server calculates the Root Mean Square (RMS) of incoming audio chunks. If the RMS exceeds `800` (meaning you are speaking), the server immediately drops the Gemini audio stream playback and sends an `event: "clear"` to Exotel to empty the playback buffer on their side.
*   **Audio Formatting**:
    *   Exotel audio is raw signed 16-bit little-endian PCM. The bridge packages outgoing audio into multiples of `320` bytes (minimum chunk `3200` bytes) before transmitting to Exotel.
