"""
AI Engine – abstracts STT, TTS, and LLM for local and cloud usage.

Local STT  : faster-whisper (CPU-based, bundled)
Local TTS  : piper-tts (CPU-based, bundled)
Local LLM  : Ollama HTTP API (existing server)
Cloud STT  : OpenAI Whisper API (or compatible)
Cloud TTS  : OpenAI TTS API (or compatible)
Cloud LLM  : OpenAI Chat API (or any OpenAI-compatible endpoint)
"""

import io
import os
import wave
try:
    import audioop                    # Python ≤ 3.12 stdlib
except ModuleNotFoundError:
    try:
        import audioop_lts as audioop  # Python 3.13+ via audioop-lts package
    except ModuleNotFoundError:
        raise RuntimeError(
            "audioop not found. Install it with: pip install audioop-lts"
        ) from None
import logging
import tempfile
import requests
import numpy as np
from math import gcd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ── Audio Helpers ─────────────────────────────────────────────────────────────

def mulaw_to_wav_bytes(pyvoip_bytes: bytes, sample_rate: int = 8000) -> bytes:
    """Convert pyVoIP 8-bit Offset-PCM to 16-bit WAV at 16 kHz for STT.

    pyVoIP liefert read_audio() als 8-bit Offset-PCM (128 = Stille).
    Schritt 1: bias(-128) → 8-bit signed
    Schritt 2: lin2lin(1→2) → 16-bit signed
    Schritt 3: resample_poly 8000 → 16000 Hz (Anti-Aliasing)
    """
    from scipy.signal import resample_poly
    pcm_signed8 = audioop.bias(pyvoip_bytes, 1, -128)
    pcm_16bit = audioop.lin2lin(pcm_signed8, 1, 2)
    g = gcd(sample_rate, 16000)
    samples = np.frombuffer(pcm_16bit, dtype=np.int16).astype(np.float32)
    resampled = resample_poly(samples, 16000 // g, sample_rate // g)
    pcm_16k = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm_16k)
    return buf.getvalue()


def wav_bytes_to_mulaw(wav_bytes: bytes, target_rate: int = 8000) -> bytes:
    """Convert WAV bytes zu 8-bit Offset-PCM für pyVoIP write_audio().

    pyVoIP erwartet intern 8-bit Offset-PCM (128 = Stille).
    encode_pcmu() macht dann bias(-128) + lin2ulaw(width=1) → RTP µ-law.

    Schritt 1: WAV lesen → 16-bit signed PCM
    Schritt 2: Mono, 16-bit sicherstellen
    Schritt 3: Anti-Aliasing-Resampling mit scipy auf target_rate
    Schritt 4: lin2lin(2→1) → 8-bit signed
    Schritt 5: bias(+128) → 8-bit Offset-PCM
    """
    buf = io.BytesIO(wav_bytes)
    with wave.open(buf, 'rb') as wf:
        src_rate = wf.getframerate()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        pcm = wf.readframes(wf.getnframes())
    if channels > 1:
        pcm = audioop.tomono(pcm, sampwidth, 0.5, 0.5)
    if sampwidth != 2:
        pcm = audioop.lin2lin(pcm, sampwidth, 2)
    # Anti-Aliasing-Resampling mit scipy
    if src_rate != target_rate:
        from scipy.signal import resample_poly
        g = gcd(src_rate, target_rate)
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        resampled = resample_poly(samples, target_rate // g, src_rate // g)
        pcm = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
    # Telefon-Tiefpassfilter 3400 Hz (Telefonband 300–3400 Hz) – entfernt Aliasing-Artefakte
    from scipy.signal import butter, sosfiltfilt
    sos = butter(6, 3400, btype='low', fs=target_rate, output='sos')
    samples_f = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    filtered = sosfiltfilt(sos, samples_f)
    # Normalisierung auf ~80 % des 8-bit-Maximalwerts für optimale Dynamiknutzung
    peak = np.abs(filtered).max()
    if peak > 0:
        filtered = filtered * (26000.0 / peak)
    pcm = np.clip(filtered, -32768, 32767).astype(np.int16).tobytes()
    # 16-bit signed → 8-bit signed → 8-bit Offset-PCM (128=Stille)
    pcm_8bit = audioop.lin2lin(pcm, 2, 1)
    pcm_offset = audioop.bias(pcm_8bit, 1, 128)
    return pcm_offset  # 8-bit Offset-PCM @ target_rate


def detect_silence(pyvoip_chunk: bytes, threshold: int = 50) -> bool:
    """Stille erkennen in pyVoIP 8-bit Offset-PCM (128=Stille).

    Erst bias(-128) → 8-bit signed, dann rms mit width=1.
    Threshold 50 entspricht ca. 300 im 16-bit-Bereich.
    """
    if not pyvoip_chunk:
        return True
    pcm_signed = audioop.bias(pyvoip_chunk, 1, -128)
    rms = audioop.rms(pcm_signed, 1)
    return rms < threshold


# ── STT ───────────────────────────────────────────────────────────────────────

_whisper_model = None


def _get_whisper_model(model_size: str = "base"):
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        logger.info(f"Loading Whisper model '{model_size}' (CPU) …")
        _whisper_model = WhisperModel(model_size, device="cpu", compute_type="int8")
        logger.info("Whisper model loaded.")
    return _whisper_model


def transcribe_local(wav_bytes: bytes, language: str = None, model_size: str = "base") -> str:
    """Transcribe audio using local faster-whisper (CPU)."""
    model = _get_whisper_model(model_size)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp_path = f.name
    try:
        kwargs = {}
        if language:
            kwargs["language"] = language
        segments, _ = model.transcribe(tmp_path, beam_size=5, **kwargs)
        text = " ".join(s.text.strip() for s in segments)
        return text.strip()
    finally:
        os.unlink(tmp_path)


def transcribe_cloud(wav_bytes: bytes, api_key: str, base_url: str = None,
                     model: str = "whisper-1", language: str = None) -> str:
    """Transcribe audio using OpenAI-compatible Whisper API."""
    import openai
    client = openai.OpenAI(api_key=api_key, base_url=base_url or "https://api.openai.com/v1")
    kwargs = {"model": model, "response_format": "text"}
    if language:
        kwargs["language"] = language
    result = client.audio.transcriptions.create(
        file=("audio.wav", io.BytesIO(wav_bytes), "audio/wav"),
        **kwargs
    )
    return str(result).strip()


def transcribe(wav_bytes: bytes, cfg: dict) -> str:
    """Dispatch to local or cloud STT based on config."""
    mode = cfg.get("stt_mode", "local")
    if mode == "local":
        return transcribe_local(
            wav_bytes,
            language=cfg.get("stt_language") or None,
            model_size=cfg.get("stt_local_model", "base")
        )
    else:
        return transcribe_cloud(
            wav_bytes,
            api_key=cfg.get("stt_cloud_api_key", ""),
            base_url=cfg.get("stt_cloud_base_url") or None,
            model=cfg.get("stt_cloud_model", "whisper-1"),
            language=cfg.get("stt_language") or None
        )


# ── TTS ───────────────────────────────────────────────────────────────────────

_piper_voice = None
_piper_voice_path = None


def synthesize_local(text: str, model_path: str, config_path: str = None,
                     length_scale: float = 1.0, noise_scale: float = 0.667,
                     noise_w: float = 0.8) -> bytes:
    """Synthesize speech using local piper-tts. Returns WAV bytes."""
    global _piper_voice, _piper_voice_path
    from piper.voice import PiperVoice
    if _piper_voice is None or _piper_voice_path != model_path:
        logger.info(f"Loading Piper TTS model: {model_path}")
        kwargs = {}
        if config_path and os.path.exists(config_path):
            kwargs["config_path"] = config_path
        _piper_voice = PiperVoice.load(model_path, **kwargs)
        _piper_voice_path = model_path
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_piper_voice.config.sample_rate)
        try:
            for chunk in _piper_voice.synthesize(
                    text, length_scale=length_scale,
                    noise_scale=noise_scale, noise_w=noise_w):
                wf.writeframes(chunk.audio_int16_bytes)
        except TypeError:
            # Ältere piper-tts Versionen ohne diese Parameter
            for chunk in _piper_voice.synthesize(text):
                wf.writeframes(chunk.audio_int16_bytes)
    return buf.getvalue()


def synthesize_cloud(text: str, api_key: str, base_url: str = None,
                     model: str = "tts-1", voice: str = "alloy",
                     speed: float = 1.0) -> bytes:
    """Synthesize speech using OpenAI-compatible TTS API. Returns WAV bytes."""
    import openai
    client = openai.OpenAI(api_key=api_key, base_url=base_url or "https://api.openai.com/v1")
    kwargs = dict(model=model, voice=voice, input=text, response_format="wav")
    if speed != 1.0:
        kwargs["speed"] = speed
    response = client.audio.speech.create(**kwargs)
    return response.content


def synthesize_elevenlabs(text: str, api_key: str,
                          voice_id: str = "21m00Tcm4TlvDq8ikWAM",
                          model_id: str = "eleven_multilingual_v2",
                          stability: float = 0.5,
                          similarity_boost: float = 0.75) -> bytes:
    """Synthesize speech using ElevenLabs TTS API. Returns WAV bytes."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": api_key, "Content-Type": "application/json",
               "Accept": "audio/mpeg"}
    body = {"text": text, "model_id": model_id,
            "voice_settings": {"stability": stability,
                                "similarity_boost": similarity_boost}}
    resp = requests.post(url, json=body, headers=headers, timeout=30)
    resp.raise_for_status()
    # MP3 → WAV via pydub
    from pydub import AudioSegment
    audio = AudioSegment.from_mp3(io.BytesIO(resp.content))
    buf = io.BytesIO()
    audio.export(buf, format="wav")
    return buf.getvalue()


def synthesize_espeak(text: str, lang: str = "de") -> bytes:
    """Fallback TTS using espeak-ng (always installed). Returns WAV bytes."""
    import subprocess
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name
    try:
        subprocess.run(
            ["espeak-ng", "-v", lang, "-w", tmp_path, text],
            check=True, capture_output=True
        )
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        os.unlink(tmp_path)


def synthesize_edge(text: str, voice: str = "de-DE-KatjaNeural") -> bytes:
    """Synthesize speech using Microsoft Edge TTS (free, no API key). Returns WAV bytes."""
    import asyncio
    import threading

    async def _synth():
        import edge_tts
        communicate = edge_tts.Communicate(text, voice)
        chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return b"".join(chunks)

    # Run in a dedicated thread with its own event loop (avoids eventlet conflicts)
    result = [None]
    error = [None]

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result[0] = loop.run_until_complete(_synth())
        except Exception as e:
            error[0] = e
        finally:
            loop.close()

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=30)
    if error[0]:
        raise error[0]
    if result[0] is None:
        raise RuntimeError("Edge TTS returned no audio")
    from pydub import AudioSegment
    audio = AudioSegment.from_mp3(io.BytesIO(result[0]))
    buf = io.BytesIO()
    audio.export(buf, format="wav")
    return buf.getvalue()


def synthesize_google(text: str, api_key: str, voice: str = "de-DE-Neural2-A",
                      language_code: str = "de-DE") -> bytes:
    """Synthesize speech using Google Cloud TTS. Returns WAV bytes.

    Free tier: 1M WaveNet/Neural2 chars/month, 4M Standard chars/month (permanent).
    API key from: console.cloud.google.com → APIs → Text-to-Speech → Credentials
    """
    url = "https://texttospeech.googleapis.com/v1/text:synthesize"
    body = {
        "input": {"text": text},
        "voice": {"languageCode": language_code, "name": voice},
        "audioConfig": {"audioEncoding": "MP3"},
    }
    resp = requests.post(url, json=body, params={"key": api_key}, timeout=30)
    resp.raise_for_status()
    import base64
    mp3_bytes = base64.b64decode(resp.json()["audioContent"])
    from pydub import AudioSegment
    audio = AudioSegment.from_mp3(io.BytesIO(mp3_bytes))
    buf = io.BytesIO()
    audio.export(buf, format="wav")
    return buf.getvalue()


def synthesize_azure(text: str, api_key: str, region: str = "westeurope",
                     voice: str = "de-DE-KatjaNeural") -> bytes:
    """Synthesize speech using Microsoft Azure Speech Service. Returns WAV bytes.

    Free tier: 500,000 characters/month (F0).
    API key from: portal.azure.com → Speech Service → Keys and Endpoint
    """
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    # Escape XML special chars in text
    import html as _html
    safe_text = _html.escape(text)
    ssml = (
        f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="de-DE">'
        f'<voice name="{voice}">{safe_text}</voice></speak>'
    )
    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "riff-24khz-16bit-mono-pcm",
    }
    resp = requests.post(url, headers=headers, data=ssml.encode("utf-8"), timeout=30)
    resp.raise_for_status()
    return resp.content  # WAV bytes (24 kHz, 16-bit mono PCM)


def synthesize(text: str, cfg: dict) -> bytes:
    """Dispatch to local, Edge, OpenAI-cloud, Azure, or ElevenLabs TTS. Returns WAV bytes."""
    mode = cfg.get("tts_mode", "local")
    if mode == "edge":
        return synthesize_edge(
            text,
            voice=cfg.get("tts_edge_voice", "de-DE-KatjaNeural"),
        )
    elif mode == "google":
        return synthesize_google(
            text,
            api_key=cfg.get("tts_google_api_key", ""),
            voice=cfg.get("tts_google_voice", "de-DE-Neural2-A"),
            language_code=cfg.get("tts_google_language_code", "de-DE"),
        )
    elif mode == "azure":
        return synthesize_azure(
            text,
            api_key=cfg.get("tts_azure_api_key", ""),
            region=cfg.get("tts_azure_region", "westeurope"),
            voice=cfg.get("tts_azure_voice", "de-DE-KatjaNeural"),
        )
    elif mode == "elevenlabs":
        return synthesize_elevenlabs(
            text,
            api_key=cfg.get("tts_elevenlabs_api_key", ""),
            voice_id=cfg.get("tts_elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM"),
            model_id=cfg.get("tts_elevenlabs_model_id", "eleven_multilingual_v2"),
            stability=float(cfg.get("tts_elevenlabs_stability", "0.5")),
            similarity_boost=float(cfg.get("tts_elevenlabs_similarity", "0.75")),
        )
    elif mode == "cloud":
        return synthesize_cloud(
            text,
            api_key=cfg.get("tts_cloud_api_key", ""),
            base_url=cfg.get("tts_cloud_base_url") or None,
            model=cfg.get("tts_cloud_model", "tts-1"),
            voice=cfg.get("tts_cloud_voice", "alloy"),
            speed=float(cfg.get("tts_cloud_speed", "1.0")),
        )
    else:  # local
        model_path = cfg.get("tts_local_model_path", "")
        if model_path and os.path.exists(model_path):
            config_path = cfg.get("tts_local_config_path", "")
            return synthesize_local(
                text, model_path, config_path or None,
                length_scale=float(cfg.get("tts_local_length_scale", "1.0")),
                noise_scale=float(cfg.get("tts_local_noise_scale", "0.667")),
                noise_w=float(cfg.get("tts_local_noise_w", "0.8")),
            )
        else:
            lang = cfg.get("stt_language", "de")
            return synthesize_espeak(text, lang)


# ── LLM ───────────────────────────────────────────────────────────────────────

def llm_ollama(system_prompt: str, conversation: list, model: str,
               base_url: str = "http://localhost:11434") -> str:
    """Call local Ollama LLM API."""
    messages = [{"role": "system", "content": system_prompt}] + conversation
    url = base_url.rstrip("/") + "/api/chat"
    resp = requests.post(url, json={"model": model, "messages": messages, "stream": False},
                         timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["message"]["content"].strip()


def llm_cloud(system_prompt: str, conversation: list, api_key: str,
              base_url: str = None, model: str = "gpt-4o-mini") -> str:
    """Call OpenAI-compatible LLM API."""
    import openai
    client = openai.OpenAI(api_key=api_key, base_url=base_url or "https://api.openai.com/v1")
    messages = [{"role": "system", "content": system_prompt}] + conversation
    response = client.chat.completions.create(model=model, messages=messages, max_tokens=500)
    return response.choices[0].message.content.strip()


def generate_response(system_prompt: str, conversation: list, cfg: dict) -> str:
    """Dispatch to local Ollama or cloud LLM, with optional agent tool-calling."""
    from agent_tools import get_tool_definitions, execute_tool

    tools = get_tool_definitions(cfg)
    if tools:
        enriched_prompt = f"{system_prompt}\n\n{_build_date_context()}"
        return _generate_with_tools(enriched_prompt, conversation, tools, cfg)

    mode = cfg.get("llm_mode", "local")
    if mode == "local":
        return llm_ollama(
            system_prompt, conversation,
            model=cfg.get("llm_local_model", "llama3"),
            base_url=cfg.get("llm_local_url", "http://localhost:11434")
        )
    else:
        return llm_cloud(
            system_prompt, conversation,
            api_key=cfg.get("llm_cloud_api_key", ""),
            base_url=cfg.get("llm_cloud_base_url") or None,
            model=cfg.get("llm_cloud_model", "gpt-4o-mini")
        )


def _build_date_context() -> str:
    """Build a date context string with the next 14 days mapped to weekdays."""
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    day_names = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    lines = [f"Aktuelles Datum und Uhrzeit: {day_names[now.weekday()]}, {now.strftime('%d.%m.%Y %H:%M')} Uhr"]
    lines.append("Datumsreferenz für Terminplanung (Wochentag → konkretes Datum):")
    for i in range(1, 15):
        day = now + timedelta(days=i)
        label = "morgen" if i == 1 else "übermorgen" if i == 2 else ""
        suffix = f" ({label})" if label else ""
        lines.append(f"  {day_names[day.weekday()]}, {day.strftime('%d.%m.%Y')}{suffix}")
    lines.append("Nutze immer das konkrete Datum (TT.MM.JJJJ) wenn du Termine anlegst.")
    return "\n".join(lines)


def _generate_with_tools(system_prompt: str, conversation: list,
                         tools: list, cfg: dict) -> str:
    """Run a tool-calling loop (max 5 iterations) and return final text."""
    import json as _json
    from agent_tools import execute_tool
    import openai

    mode = cfg.get("llm_mode", "local")
    if mode == "local":
        base_url = cfg.get("llm_local_url", "http://localhost:11434").rstrip("/") + "/v1"
        api_key = "ollama"
        model = cfg.get("llm_local_model", "llama3")
    else:
        base_url = cfg.get("llm_cloud_base_url") or "https://api.openai.com/v1"
        api_key = cfg.get("llm_cloud_api_key", "")
        model = cfg.get("llm_cloud_model", "gpt-4o-mini")

    client = openai.OpenAI(api_key=api_key, base_url=base_url)
    messages = [{"role": "system", "content": system_prompt}] + list(conversation)

    for _ in range(5):  # max iterations
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=600,
            )
        except Exception as e:
            # Model may not support tools — fall back to plain call
            logger.warning(f"Tool-calling failed ({e}), falling back to plain LLM")
            response = client.chat.completions.create(
                model=model, messages=messages, max_tokens=600
            )
            return response.choices[0].message.content.strip()

        msg = response.choices[0].message

        # No tool calls → return final text
        if not msg.tool_calls:
            return (msg.content or "").strip()

        # Execute all requested tool calls
        messages.append({"role": "assistant", "content": msg.content,
                          "tool_calls": [
                              {"id": tc.id, "type": "function",
                               "function": {"name": tc.function.name,
                                            "arguments": tc.function.arguments}}
                              for tc in msg.tool_calls
                          ]})

        for tc in msg.tool_calls:
            try:
                args = _json.loads(tc.function.arguments)
            except Exception:
                args = {}
            result = execute_tool(tc.function.name, args, cfg)
            logger.info(f"Tool {tc.function.name} result: {result[:200]}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    # Exceeded max iterations — do a final plain call
    final = client.chat.completions.create(model=model, messages=messages, max_tokens=600)
    return (final.choices[0].message.content or "").strip()


def summarize_call(transcript: str, cfg: dict) -> str:
    """Generate a short summary of a call transcript."""
    system = (
        "Du bist ein Assistent, der Anruftranskripte prägnant zusammenfasst. "
        "Fasse den folgenden Anruf in 2-3 Sätzen zusammen. Extrahiere das Hauptanliegen."
    )
    conversation = [{"role": "user", "content": f"Transkript:\n{transcript}"}]
    try:
        return generate_response(system, conversation, cfg)
    except Exception as e:
        logger.error(f"Summarize failed: {e}")
        return transcript[:300]
