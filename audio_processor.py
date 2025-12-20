"""
Audio Processing Utilities
Handles audio format conversions between Twilio (μ-law 8kHz) and Sarvam (PCM 16kHz)
"""
import audioop
import math
import io
import wave
from typing import Optional


class AudioProcessor:
    """Handle audio format conversions for voice agent pipeline"""
    
    @staticmethod
    def mulaw_to_pcm16(mulaw_data: bytes, sample_rate: int = 8000) -> bytes:
        """
        Convert μ-law encoded audio to 16-bit PCM
        
        Args:
            mulaw_data: μ-law encoded audio bytes
            sample_rate: Sample rate (default 8000 for Twilio)
            
        Returns:
            16-bit PCM audio bytes
        """
        return audioop.ulaw2lin(mulaw_data, 2)
    
    @staticmethod
    def pcm16_to_mulaw(pcm_data: bytes) -> bytes:
        """
        Convert 16-bit PCM to μ-law encoding
        
        Args:
            pcm_data: 16-bit PCM audio bytes
            
        Returns:
            μ-law encoded audio bytes
        """
        return audioop.lin2ulaw(pcm_data, 2)
    
    @staticmethod
    def resample_audio(audio_data: bytes, from_rate: int, to_rate: int, sample_width: int = 2) -> bytes:
        """
        Resample audio from one sample rate to another
        
        Args:
            audio_data: Input audio bytes
            from_rate: Source sample rate
            to_rate: Target sample rate
            sample_width: Sample width in bytes (default 2 for 16-bit)
            
        Returns:
            Resampled audio bytes
        """
        if from_rate == to_rate:
            return audio_data
        
        return audioop.ratecv(audio_data, sample_width, 1, from_rate, to_rate, None)[0]
    
    @staticmethod
    def mulaw_8k_to_pcm16_16k(mulaw_data: bytes) -> bytes:
        """
        Convert Twilio μ-law 8kHz to Sarvam PCM 16kHz
        
        Args:
            mulaw_data: μ-law encoded audio at 8kHz
            
        Returns:
            16-bit PCM audio at 16kHz
        """
        # Step 1: μ-law to 16-bit PCM at 8kHz
        pcm_8k = AudioProcessor.mulaw_to_pcm16(mulaw_data, 8000)
        
        # Step 2: Resample from 8kHz to 16kHz
        pcm_16k = AudioProcessor.resample_audio(pcm_8k, 8000, 16000, 2)
        
        return pcm_16k
    
    @staticmethod
    def pcm16_16k_to_mulaw_8k(pcm_data: bytes) -> bytes:
        """
        Convert Sarvam PCM 16kHz to Twilio μ-law 8kHz
        
        Args:
            pcm_data: 16-bit PCM audio at 16kHz
            
        Returns:
            μ-law encoded audio at 8kHz
        """
        # Step 1: Resample from 16kHz to 8kHz
        pcm_8k = AudioProcessor.resample_audio(pcm_data, 16000, 8000, 2)
        
        # Step 2: 16-bit PCM to μ-law
        mulaw = AudioProcessor.pcm16_to_mulaw(pcm_8k)
        
        return mulaw
    @staticmethod
    def wav_to_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
        """
        Extract raw PCM data and sample rate from WAV file bytes

        Returns:
            (pcm_bytes, sample_rate_hz)
        """
        try:
            with wave.open(io.BytesIO(wav_bytes), 'rb') as wav_file:
                sample_rate = wav_file.getframerate()
                pcm_data = wav_file.readframes(wav_file.getnframes())
            return pcm_data, sample_rate
        except Exception:
            # If it's already raw PCM and we don't know the rate, assume 16000
            return wav_bytes, 16000

    @staticmethod
    def wav_to_pcm(wav_bytes: bytes) -> tuple[bytes, int]:
        """
        Extract raw PCM data and sample rate from WAV bytes.

        Returns:
            (pcm_bytes, sample_rate_hz)
        """
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
                sample_rate = wav_file.getframerate()
                pcm_data = wav_file.readframes(wav_file.getnframes())
            return pcm_data, sample_rate
        except Exception:
            # If for some reason it's already raw PCM or header is broken,
            # just return as-is and assume 16000 Hz
            return wav_bytes, 16000

    @staticmethod
    def generate_test_tone_mulaw(duration_sec: float = 1.0, freq: float = 440.0, sample_rate: int = 8000) -> bytes:
        """
        Generate a test tone in μ-law format (for debugging Twilio audio path)
        
        Args:
            duration_sec: Duration in seconds
            freq: Frequency in Hz (default 440Hz = A4)
            sample_rate: Sample rate in Hz
            
        Returns:
            μ-law encoded test tone
        """
        samples = []
        for n in range(int(duration_sec * sample_rate)):
            t = n / sample_rate
            value = int(16000 * math.sin(2 * math.pi * freq * t))
            samples.append(value.to_bytes(2, byteorder="little", signed=True))
        
        pcm16 = b"".join(samples)
        return audioop.lin2ulaw(pcm16, 2)
    
    @staticmethod
    def calculate_audio_duration(audio_bytes: bytes, sample_rate: int, sample_width: int = 2, channels: int = 1) -> float:
        """
        Calculate duration of audio in seconds
        
        Args:
            audio_bytes: Audio data
            sample_rate: Sample rate in Hz
            sample_width: Sample width in bytes
            channels: Number of channels
            
        Returns:
            Duration in seconds
        """
        num_samples = len(audio_bytes) // (sample_width * channels)
        return num_samples / sample_rate
    
    @staticmethod
    def adjust_volume(audio_data: bytes, factor: float, sample_width: int = 2) -> bytes:
        """
        Adjust audio volume by a factor
        
        Args:
            audio_data: Input audio bytes
            factor: Volume multiplier (e.g., 0.5 for half volume, 2.0 for double)
            sample_width: Sample width in bytes
            
        Returns:
            Volume-adjusted audio bytes
        """
        return audioop.mul(audio_data, sample_width, factor)
    
    @staticmethod
    def mix_audio(audio1: bytes, audio2: bytes, sample_width: int = 2) -> bytes:
        """
        Mix two audio streams together
        
        Args:
            audio1: First audio stream
            audio2: Second audio stream
            sample_width: Sample width in bytes
            
        Returns:
            Mixed audio bytes
        """
        # Ensure both are same length by padding shorter one with silence
        if len(audio1) < len(audio2):
            audio1 += b'\x00' * (len(audio2) - len(audio1))
        elif len(audio2) < len(audio1):
            audio2 += b'\x00' * (len(audio1) - len(audio2))
        
        return audioop.add(audio1, audio2, sample_width)
    
    @staticmethod
    def detect_silence(audio_data: bytes, threshold: int = 500, sample_width: int = 2) -> bool:
        """
        Detect if audio contains mostly silence
        
        Args:
            audio_data: Audio bytes to analyze
            threshold: RMS threshold below which is considered silence
            sample_width: Sample width in bytes
            
        Returns:
            True if audio is mostly silent, False otherwise
        """
        try:
            rms = audioop.rms(audio_data, sample_width)
            return rms < threshold
        except Exception:
            return True
    
    @staticmethod
    def normalize_audio(audio_data: bytes, target_peak: int = 32000, sample_width: int = 2) -> bytes:
        """
        Normalize audio to a target peak level
        
        Args:
            audio_data: Input audio bytes
            target_peak: Target peak amplitude (max 32767 for 16-bit)
            sample_width: Sample width in bytes
            
        Returns:
            Normalized audio bytes
        """
        try:
            max_amplitude = audioop.max(audio_data, sample_width)
            if max_amplitude == 0:
                return audio_data
            
            factor = target_peak / max_amplitude
            return audioop.mul(audio_data, sample_width, factor)
        except Exception:
            return audio_data