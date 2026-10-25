"""Nano-compatible stream validation and duration units from the final file."""
from fractions import Fraction
import math

RATES = {8000,11025,12000,16000,22050,24000,32000,44100,48000}


def stream_info(info):
    audio=next((s for s in info.get('streams',[]) if s.get('codec_type')=='audio'),None)
    if audio is None:raise ValueError('File has no audio stream.')
    rate=int(audio.get('sample_rate') or 0);channels=int(audio.get('channels') or 0)
    if rate<=0 or channels<=0:raise ValueError('Cannot determine audio sample rate/channels.')
    if audio.get('duration_ts') is not None and audio.get('time_base'):
        duration=float(int(audio['duration_ts'])*Fraction(audio['time_base']))
    else:duration=float(audio.get('duration') or info.get('format',{}).get('duration') or 0)
    if not math.isfinite(duration) or duration<=0:raise ValueError('Invalid or missing audio duration.')
    return audio,rate,channels,duration,round(duration*rate)


def validate_audio(info, allow_mp3=True):
    audio,rate,channels,_,_=stream_info(info)
    codec=audio.get('codec_name');bitrate=int(audio.get('bit_rate') or 0)
    bits=int(audio.get('bits_per_raw_sample') or audio.get('bits_per_sample') or 0)
    valid = ((allow_mp3 and codec=='mp3' and 0<bitrate<=320000)
             or (codec=='aac' and audio.get('profile')=='LC' and 0<bitrate<=320000)
             or (codec=='alac' and 0<bits<=16))
    if not valid or rate not in RATES or channels not in (1,2):
        raise ValueError('Audio is not Nano-compatible MP3, AAC-LC, or 16-bit ALAC.')
    return audio
