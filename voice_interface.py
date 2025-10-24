import os
import json
import uuid
import math
import time
import numpy as np
import resampy
import sounddevice as sd
import speech_recognition as sr
import pvporcupine
from dotenv import load_dotenv
from core.context import logger

# --- Configuration ---
load_dotenv()
PICOVOICE_KEY = os.getenv("PICOVOICE_ACCESS_KEY")
WAKE_WORD_PATH = "apollo.ppn"
# MODIFIED: We will search for this name instead of using a hardcoded index
TARGET_MIC_NAME = "Yeti Stereo Microphone"
MIC_CHANNELS = 2
MIC_SAMPLE_RATE = 44100

def find_mic_device_index(device_name: str) -> int | None:
    """Searches for an audio device by name and returns its index."""
    logger.info(f"Searching for microphone: '{device_name}'...")
    devices = sd.query_devices()
    for index, device in enumerate(devices):
        if device_name.lower() in device['name'].lower() and device['max_input_channels'] > 0:
            logger.info(f"Found '{device['name']}' at index {index}.")
            return index
    logger.error(f"Microphone '{device_name}' not found.")
    return None

def listen_for_command(recognizer, mic, queue):
    """Listens for a command and puts the transcribed text onto the queue."""
    logger.info("👂 Listening for your command...")
    sd.play(sd.sin(440, samplerate=44100, duration=0.2), samplerate=44100)
    try:
        with mic as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.5)
            audio = recognizer.listen(source, timeout=5, phrase_time_limit=10)
        
        command = recognizer.recognize_google(audio)
        logger.info(f"🎤 Transcribed: '{command}'. Sending to main process.")
        queue.put(command)

    except Exception as e:
        logger.error(f"Error during command listening: {e}")

def main(command_queue):
    """Main loop for the voice interface process."""
    porcupine = None
    try:
        # MODIFIED: Find the mic index first
        mic_device_index = find_mic_device_index(TARGET_MIC_NAME)
        if mic_device_index is None:
            return # Exit if the mic can't be found

        porcupine = pvporcupine.create(access_key=PICOVOICE_KEY, keyword_paths=[WAKE_WORD_PATH])
        TARGET_RATE = porcupine.sample_rate
        mic = sr.Microphone(device_index=mic_device_index, sample_rate=MIC_SAMPLE_RATE)
        recognizer = sr.Recognizer()

        logger.info("--- 🚀 VOICE PROCESS ACTIVATED ---")
        logger.info(f"🎯 Listening for wake word: 'Apollo'...")

        while True:
            pcm_stereo = sd.rec(
                frames=math.ceil((MIC_SAMPLE_RATE / TARGET_RATE) * porcupine.frame_length),
                samplerate=MIC_SAMPLE_RATE, channels=MIC_CHANNELS,
                dtype='int16', device=mic_device_index
            )
            sd.wait()
            pcm_mono = np.mean(pcm_stereo, axis=1).astype(np.int16)
            pcm_float = pcm_mono.astype(float) / 32768.0
            pcm_resampled = resampy.resample(pcm_float, MIC_SAMPLE_RATE, TARGET_RATE)
            pcm_porcupine_format = (pcm_resampled * 32768.0).astype(np.int16)

            keyword_index = porcupine.process(pcm_porcupine_format)
            if keyword_index >= 0:
                logger.info("✨ Wake word detected!")
                listen_for_command(recognizer, mic, command_queue)
                logger.info(f"🎯 Listening for wake word: 'Apollo'...")

    except Exception as e:
        logger.error(f"Fatal error in voice process: {e}")
    finally:
        if porcupine is not None:
            porcupine.delete()