import os
import json
import time
import numpy as np
import resampy
import sounddevice as sd
import pvporcupine
import asyncio
import math
import logging # Required for QueueHandler
from dotenv import load_dotenv
from google import genai
from google.genai import types

# --- Local Imports ---
# Note: In a new process, these imports initialize new instances of their modules.
from core.context import logger as local_logger # We will override this logger's handlers
from utils.database import get_user_profile, get_archived_goals, update_user_profile, get_active_goals
from core.agent_profile import get_agent_profile
from core.tools import TOOL_MANIFEST

# --- Tool Imports ---
from utils.goal_manager import create_and_add_goal
from utils.calendar_client import get_upcoming_events
from utils.email_client import create_draft as draft_email_tool
from core.tools import read_file_tool

# --- Configuration ---
load_dotenv()
PICOVOICE_KEY = os.getenv("PICOVOICE_ACCESS_KEY")
WAKE_WORD_PATH = "gemini.ppn"
TARGET_MIC_NAME = "Yeti Stereo Microphone" # Keeping logic, but user should note to change this if needed

MIC_SAMPLE_RATE = 44100
MIC_CHANNELS = 2

WAKE_WORD_REQUIRED_RATE = 16000
WAKE_WORD_CHANNELS = 1

LIVE_API_INPUT_SAMPLE_RATE = 16000
LIVE_API_OUTPUT_SAMPLE_RATE = 24000
LIVE_API_CHANNELS = 1
LIVE_API_BLOCK_SIZE = int(MIC_SAMPLE_RATE * 0.1)

# --- Audio Streaming State ---
is_live_conversation = False
audio_queue = asyncio.Queue()
previous_session_handle: str | None = None
last_speech_time = None

# --- Logging Setup for Child Process ---
class QueueHandler(logging.Handler):
    """Custom handler to send logs to the multiprocessing queue."""
    def __init__(self, queue):
        super().__init__()
        self.queue = queue

    def emit(self, record):
        try:
            msg = self.format(record)
            self.queue.put(msg)
        except Exception:
            self.handleError(record)

def setup_process_logging(status_queue):
    """Attaches the MP Queue handler to the local logger."""
    if status_queue:
        handler = QueueHandler(status_queue)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - [VOICE] - %(message)s'))
        local_logger.addHandler(handler)
        local_logger.info("Voice Interface logging connected to Dashboard.")

def find_mic_device_indices(device_name: str) -> list[int]:
    """Searches for all audio devices matching the name and returns their indices."""
    local_logger.info(f"VOICE: Searching for all microphone indices matching: '{device_name}'...")
    matching_indices = []
    try:
        devices = sd.query_devices()
        for index, device in enumerate(devices):
            if device_name.lower() in device['name'].lower() and device['max_input_channels'] > 0:
                matching_indices.append(index)
    except Exception as e:
        local_logger.error(f"VOICE: Error querying devices: {e}")
    
    # Fallback if no specific mic is found
    if not matching_indices:
        local_logger.warning(f"VOICE: '{device_name}' not found. Using default input device.")
        default_device = sd.default.device[0]
        if default_device is not None:
             matching_indices.append(default_device)
             local_logger.info(f"VOICE: Defaulting to Index {default_device}")

    return matching_indices

def get_live_chat_context() -> str:
    profile = get_user_profile()
    profile_str = json.dumps(profile, indent=2) if profile else "No profile data exists yet."
    recent_tasks = get_archived_goals(page=1, per_page=3)
    tasks_str = "\n".join([f"- {g['goal']} (Status: {g['status']})" for g in recent_tasks]) if recent_tasks else "No recent tasks."
    agent_profile = get_agent_profile(for_planner=False) 

    return f"""
    You are Cognito, a proactive AI partner. You are in a LIVE VOICE CHAT with the user.
    Your primary goal is to have a natural, fluid, and friendly conversation.
    
    **YOUR CONVERSATIONAL MANDATES:**
    1.  Be conversational, friendly, and natural.
    2.  If the user gives you a **complex request** (like "research...", "write a report...", "analyze..."), you **MUST NOT** try to do it yourself.
    3.  Instead, you **MUST** delegate it by calling the `add_asynchronous_goal` tool. 
    
    **--- CONTEXT: YOUR AGENT PROFILE ---**
    {agent_profile}
    **--- CONTEXT: USER PROFILE ---**
    {profile_str}
    **--- CONTEXT: RECENT TASKS ---**
    {tasks_str}
    """

def get_live_chat_tools() -> list[types.Tool]:
    chat_tool_list = [types.Tool(google_search=types.GoogleSearch())]
    LIVE_TOOL_NAMES = ["update_user_profile", "draft_email", "read_file"]
    function_declarations = []
    
    for tool_def in TOOL_MANIFEST:
        if tool_def['tool_name'] in LIVE_TOOL_NAMES:
            func = types.FunctionDeclaration(name=tool_def['tool_name'], description=tool_def['description'])
            schema_properties = {}
            required_params = []
            for param in tool_def.get('parameters', []):
                param_name = param['name']
                type_mapping = {"string": "STRING", "number": "NUMBER", "integer": "INTEGER"}
                schema_properties[param_name] = types.Schema(
                    type=type_mapping.get(param['type'], "STRING"),
                    description=param.get('description')
                )
                required_params.append(param_name)
            func.parameters = types.Schema(type="OBJECT", properties=schema_properties, required=required_params)
            function_declarations.append(func)

    function_declarations.append(types.FunctionDeclaration(
        name="add_asynchronous_goal",
        description="Delegates any complex, multi-step task, research request, or report-writing goal to the main asynchronous agent.",
        parameters=types.Schema(type="OBJECT", properties={"goal_text": types.Schema(type="STRING", description="The full, natural language text of the user's goal.")}, required=["goal_text"])
    ))
    function_declarations.append(types.FunctionDeclaration(name="get_calendar_events", description="Checks the user's Google Calendar.", parameters=types.Schema(type="OBJECT", properties={})))
    function_declarations.append(types.FunctionDeclaration(name="get_active_goal_status", description="Reports on background agent status.", parameters=types.Schema(type="OBJECT", properties={})))

    if function_declarations:
        chat_tool_list.append(types.Tool(function_declarations=function_declarations))
    return chat_tool_list

def audio_input_callback(indata, frames, time_info, status):
    global is_live_conversation
    if not is_live_conversation: return
    try:
        pcm_mono = np.mean(indata, axis=1).astype(np.int16)
        pcm_float = pcm_mono.astype(float) / 32768.0
        pcm_resampled = resampy.resample(pcm_float, MIC_SAMPLE_RATE, LIVE_API_INPUT_SAMPLE_RATE)
        pcm_16k_mono = (pcm_resampled * 32768.0).astype(np.int16)
        audio_queue.put_nowait(pcm_16k_mono.tobytes())
    except asyncio.QueueFull: pass 

def _generate_chime(stream: sd.OutputStream, frequency=660, duration=0.1):
    try:
        t = np.linspace(0., duration, int(LIVE_API_OUTPUT_SAMPLE_RATE * duration), endpoint=False)
        amplitude = np.iinfo(np.int16).max * 0.3
        wave_data = (amplitude * np.sin(2. * np.pi * frequency * t)).astype(np.int16)
        stream.write(wave_data)
    except Exception: pass

async def run_live_conversation(mic_index: int):
    global is_live_conversation, audio_queue, previous_session_handle, last_speech_time
    is_live_conversation = True
    audio_queue = asyncio.Queue() 
    last_speech_time = None 
    
    local_logger.info("VOICE: Live conversation starting...")
    client = genai.Client()
    live_tools = get_live_chat_tools()
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config={"voice_config": {"prebuilt_voice_config": {"voice_name": "Kore"}}},
        realtime_input_config={"automatic_activity_detection": {"silence_duration_ms": 1500}},
        tools=live_tools,
        session_resumption={"handle": previous_session_handle},
        system_instruction=get_live_chat_context()
    )

    input_stream = None
    output_stream = None 

    try:
        output_stream = sd.OutputStream(samplerate=LIVE_API_OUTPUT_SAMPLE_RATE, channels=1, dtype='int16')
        output_stream.start()
        _generate_chime(output_stream, 660, 0.1)
        
        async with client.aio.live.connect(model="gemini-2.5-flash-native-audio-preview-09-2025", config=config) as session:
            local_logger.info("VOICE: Connected. SPEAK NOW.")
            _generate_chime(output_stream, 440, 0.1)
            last_speech_time = time.time()
            audio_queue = asyncio.Queue()

            def sync_audio_callback(indata, frames, time_info, status):
                if not is_live_conversation: return
                try:
                    pcm_mono = np.mean(indata, axis=1).astype(np.int16)
                    pcm_float = pcm_mono.astype(float) / 32768.0
                    pcm_resampled = resampy.resample(pcm_float, MIC_SAMPLE_RATE, LIVE_API_INPUT_SAMPLE_RATE)
                    pcm_16k_mono = (pcm_resampled * 32768.0).astype(np.int16)
                    audio_queue.put_nowait(pcm_16k_mono.tobytes())
                except asyncio.QueueFull: pass
            
            input_stream = sd.InputStream(samplerate=MIC_SAMPLE_RATE, blocksize=LIVE_API_BLOCK_SIZE, device=mic_index, channels=MIC_CHANNELS, dtype='int16', callback=sync_audio_callback)
            input_stream.start()
            
            async def send_task():
                while is_live_conversation:
                    try:
                        audio_bytes = await audio_queue.get()
                        await session.send_realtime_input(audio=types.Blob(data=audio_bytes, mime_type=f'audio/pcm;rate={LIVE_API_INPUT_SAMPLE_RATE}'))
                    except asyncio.CancelledError: break
                    except Exception: break

            async def receive_task():
                global is_live_conversation, previous_session_handle, last_speech_time
                try:
                    async for response in session.receive():
                        if response.data is not None:
                            last_speech_time = time.time()
                            output_stream.write(np.frombuffer(response.data, dtype='int16'))
                        
                        if response.session_resumption_update and response.session_resumption_update.new_handle:
                            previous_session_handle = response.session_resumption_update.new_handle
                        
                        if response.server_content:
                            if response.server_content.input_transcription and response.server_content.input_transcription.text:
                                local_logger.info(f"VOICE: User said: \"{response.server_content.input_transcription.text}\"")
                                last_speech_time = time.time()

                            if response.server_content.model_turn:
                                for part in response.server_content.model_turn.parts:
                                    if part.function_call:
                                        fc = part.function_call
                                        tool_params = dict(fc.args)
                                        local_logger.info(f"VOICE: Agent calling tool: {fc.name}")
                                        
                                        # --- EXECUTE TOOL ---
                                        tool_result = {"status": "error", "message": "Unknown tool"}
                                        try:
                                            if fc.name == 'update_user_profile':
                                                update_user_profile(tool_params.get('key'), tool_params.get('value'), "live_chat")
                                                tool_result = {"status": "success"}
                                            elif fc.name == 'add_asynchronous_goal':
                                                create_and_add_goal(tool_params.get('goal_text'), source="voice")
                                                tool_result = {"status": "success", "message": "Task delegated to background agent."}
                                            elif fc.name == 'get_calendar_events':
                                                tool_result = {"status": "success", "events": get_upcoming_events()}
                                            elif fc.name == 'draft_email':
                                                tool_result = {"status": "success", "message": draft_email_tool(tool_params.get('to'), tool_params.get('subject'), tool_params.get('body'))}
                                            elif fc.name == 'get_active_goal_status':
                                                active = get_active_goals()
                                                status_msg = "No active tasks." if not active else ", ".join([f"{g['goal']}: {g['status']}" for g in active])
                                                tool_result = {"status": "success", "message": status_msg}
                                            elif fc.name == 'read_file':
                                                tool_result = {"status": "success", "content": read_file_tool(tool_params.get('filename'))}
                                        except Exception as e:
                                            tool_result = {"status": "error", "message": str(e)}

                                        await session.send_tool_call_response(types.ToolCallResponse(id=fc.id, output=tool_result))
                except asyncio.CancelledError: pass
                except Exception as e: local_logger.error(f"VOICE: Receive error: {e}")
                finally: is_live_conversation = False 
            
            send_task_handle = asyncio.create_task(send_task())
            receive_task_handle = asyncio.create_task(receive_task())
            
            while is_live_conversation:
                if last_speech_time and (time.time() - last_speech_time) > 10:
                    is_live_conversation = False 
                    break
                await asyncio.sleep(0.1)
            
            send_task_handle.cancel()
            receive_task_handle.cancel()

    except Exception as e:
        local_logger.error(f"VOICE: Live conversation error: {e}")
    finally:
        if input_stream: input_stream.stop(); input_stream.close()
        if output_stream: output_stream.stop(); output_stream.close()
        is_live_conversation = False

# --- main() function (Wake Word Loop) ---
# No changes are needed to the main() function. Its logic of:
# 1. Listen for "Apollo"
# 2. Close wake-word stream
# 3. Call asyncio.run(run_live_conversation(...))
# 4. Re-open wake-word stream
# ...is still perfectly valid. The "bridge" from sync to async is correct.
def main(status_queue=None): 
    """
    Main loop for the voice interface process.
    """
    # 1. Setup logging bridge if queue provided
    if status_queue:
        setup_process_logging(status_queue)

    local_logger.info("--- VOICE PROCESS STARTED ---")
    
    porcupine = None
    wake_word_stream = None
    
    try:
        potential_indices = find_mic_device_indices(TARGET_MIC_NAME)
        if not potential_indices: return

        porcupine = pvporcupine.create(access_key=PICOVOICE_KEY, keyword_paths=[WAKE_WORD_PATH], sensitivities=[0.7])
        live_mic_index = potential_indices[0] # Just take the first match for now

        resample_factor = MIC_SAMPLE_RATE / WAKE_WORD_REQUIRED_RATE
        frames_to_read = math.ceil(porcupine.frame_length * resample_factor)
        
        while True:
            if not is_live_conversation:
                # Simple loop to reopen stream if closed
                wake_word_stream = sd.InputStream(samplerate=MIC_SAMPLE_RATE, blocksize=frames_to_read, device=live_mic_index, channels=MIC_CHANNELS, dtype='int16')
                wake_word_stream.start()
                
                while not is_live_conversation:
                    pcm_data, overflow = wake_word_stream.read(frames_to_read)
                    pcm_mono = np.mean(pcm_data, axis=1).astype(np.int16)
                    pcm_float = pcm_mono.astype(float) / 32768.0
                    pcm_resampled = resampy.resample(pcm_float, MIC_SAMPLE_RATE, WAKE_WORD_REQUIRED_RATE)
                    pcm_porcupine = (pcm_resampled * 32768.0).astype(np.int16)
                    
                    if porcupine.process(pcm_porcupine) >= 0:
                        local_logger.info("VOICE: Wake word detected!")
                        wake_word_stream.stop()
                        wake_word_stream.close()
                        asyncio.run(run_live_conversation(mic_index=live_mic_index))
                        break 
            time.sleep(0.1)

    except Exception as e:
        local_logger.error(f"Fatal error in voice process: {e}", exc_info=True)
    finally:
        if porcupine: porcupine.delete()