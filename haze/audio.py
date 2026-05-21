from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from .config import HazeConfig, OutputIcecastConfig, OutputRtpConfig, OutputSoundcardConfig, OutputUdpConfig
from .metadata import TrackMetadata
from .playlist import Track

log = logging.getLogger(__name__)

PCM_BYTES_PER_SAMPLE = 2
DEFAULT_SOURCE_QUEUE_SIZE = 12
DEFAULT_SINK_QUEUE_SIZE = 8


@dataclass(frozen=True)
class StreamMetadata:
    title: str
    artist: str
    album: str
    path: str

    @property
    def display_title(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or Path(self.path).stem

    @classmethod
    def from_track(cls, track: Track, metadata: TrackMetadata) -> StreamMetadata:
        return cls(
            title=metadata.title or track.title or track.path.stem,
            artist=metadata.artist or "",
            album=metadata.album or "",
            path=str(track.path),
        )


class AudioSink(ABC):
    def __init__(self, name: str, sample_rate: int, channels: int, block_frames: int, queue_size: int = DEFAULT_SINK_QUEUE_SIZE):
        self.name = name
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_frames = block_frames
        self._queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=queue_size)
        self._task: Optional[asyncio.Task[None]] = None
        self._current_track: Optional[StreamMetadata] = None

    async def start(self):
        await self.on_start()
        self._task = asyncio.create_task(self._run(), name=f"haze-{self.name}-sink")

    async def stop(self):
        if not self._task:
            return
        self._offer_stop()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        await self.on_stop()

    async def enqueue(self, pcm: bytes):
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        await self._queue.put(pcm)

    async def flush(self):
        while True:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                continue
            break

    async def set_track(self, metadata: StreamMetadata):
        self._current_track = metadata
        await self.on_track_change(metadata)

    async def _run(self):
        while True:
            packet = await self._queue.get()
            if packet is None:
                break
            try:
                await self.handle_audio(packet)
            except Exception as exc:
                log.error("%s sink write failed: %s", self.name, exc)

    def _offer_stop(self):
        while True:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)
                return
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

    async def on_start(self):
        return None

    async def on_stop(self):
        return None

    async def on_track_change(self, metadata: StreamMetadata):
        return None

    @abstractmethod
    async def handle_audio(self, pcm: bytes):
        raise NotImplementedError


class SoundcardSink(AudioSink):
    def __init__(self, cfg: OutputSoundcardConfig, sample_rate: int, channels: int, block_frames: int):
        super().__init__("soundcard", sample_rate, channels, block_frames)
        self.cfg = cfg
        self._stream = None

    async def on_start(self):
        import sounddevice as sd

        self._stream = await asyncio.to_thread(
            self._open_stream,
            sd,
        )
        log.info("Soundcard sink active.")

    async def on_stop(self):
        if not self._stream:
            return
        stream = self._stream
        self._stream = None
        await asyncio.to_thread(stream.stop)
        await asyncio.to_thread(stream.close)

    async def handle_audio(self, pcm: bytes):
        if not self._stream:
            return
        await asyncio.to_thread(self._stream.write, pcm)

    def _open_stream(self, sounddevice_module):
        stream = sounddevice_module.RawOutputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            blocksize=self.block_frames,
            device=self.cfg.device,
        )
        stream.start()
        return stream


class FFmpegSink(AudioSink, ABC):
    def __init__(self, name: str, sample_rate: int, channels: int, block_frames: int):
        super().__init__(name, sample_rate, channels, block_frames)
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None

    async def on_start(self):
        await self._ensure_process()

    async def on_stop(self):
        if self._stderr_task:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
            self._stderr_task = None
        if not self._process:
            return
        process = self._process
        self._process = None
        if process.stdin:
            process.stdin.close()
            with contextlib.suppress(Exception):
                await process.stdin.wait_closed()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=2)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

    async def handle_audio(self, pcm: bytes):
        await self._ensure_process()
        if not self._process or not self._process.stdin:
            return
        try:
            self._process.stdin.write(pcm)
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionError) as exc:
            log.error("%s sink lost ffmpeg process: %s", self.name, exc)
            await self._restart_process()

    async def _ensure_process(self):
        if self._process and self._process.returncode is None:
            return
        await self._spawn_process()
        if self._current_track:
            await self.on_track_change(self._current_track)

    async def _restart_process(self):
        await self.on_stop()
        await self._spawn_process()
        if self._current_track:
            await self.on_track_change(self._current_track)

    async def _spawn_process(self):
        command = self.build_command()
        log.info("Starting %s sink via ffmpeg.", self.name)
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if self._stderr_task:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task
        self._stderr_task = asyncio.create_task(self._log_stderr(), name=f"haze-{self.name}-stderr")

    async def _log_stderr(self):
        if not self._process or not self._process.stderr:
            return
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="ignore").strip()
            if text:
                log.warning("%s sink: %s", self.name, text)

    def _base_command(self) -> list[str]:
        return [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(self.sample_rate),
            "-ac",
            str(self.channels),
            "-i",
            "pipe:0",
        ]

    def _encoding_args(self, codec: str, bitrate: str, fmt: str) -> list[str]:
        args = ["-c:a", codec]
        if bitrate:
            args.extend(["-b:a", bitrate])
        args.extend(["-f", fmt])
        return args

    @abstractmethod
    def build_command(self) -> list[str]:
        raise NotImplementedError


class IcecastSink(FFmpegSink):
    def __init__(self, cfg: OutputIcecastConfig, sample_rate: int, channels: int, block_frames: int):
        super().__init__("icecast", sample_rate, channels, block_frames)
        self.cfg = cfg

    def build_command(self) -> list[str]:
        command = self._base_command()
        command.extend(self._encoding_args(self.cfg.codec, self.cfg.bitrate, self.cfg.format))
        command.extend(["-content_type", self.cfg.content_type])
        if self.cfg.name:
            command.extend(["-ice_name", self.cfg.name])
        if self.cfg.description:
            command.extend(["-ice_description", self.cfg.description])
        if self.cfg.genre:
            command.extend(["-ice_genre", self.cfg.genre])
        command.extend(["-ice_public", "1" if self.cfg.public else "0"])
        command.append(self._stream_url())
        return command

    async def on_track_change(self, metadata: StreamMetadata):
        if not self.cfg.metadata_enabled:
            return
        if not self.cfg.admin_username or not self.cfg.admin_password:
            return
        await asyncio.to_thread(self._update_metadata, metadata.display_title)

    def _stream_url(self) -> str:
        mount = self.cfg.mount if self.cfg.mount.startswith("/") else f"/{self.cfg.mount}"
        username = quote(self.cfg.username, safe="")
        password = quote(self.cfg.password, safe="")
        return f"icecast://{username}:{password}@{self.cfg.host}:{self.cfg.port}{mount}"

    def _update_metadata(self, song: str):
        mount = self.cfg.mount if self.cfg.mount.startswith("/") else f"/{self.cfg.mount}"
        query = urlencode({"mount": mount, "mode": "updinfo", "song": song})
        request = Request(
            f"http://{self.cfg.host}:{self.cfg.port}/admin/metadata?{query}",
            headers={"Authorization": f"Basic {self._admin_auth()}"},
        )
        with urlopen(request, timeout=5) as response:
            response.read()

    def _admin_auth(self) -> str:
        token = f"{self.cfg.admin_username}:{self.cfg.admin_password}".encode("utf-8")
        return base64.b64encode(token).decode("ascii")


class UdpSink(FFmpegSink):
    def __init__(self, cfg: OutputUdpConfig, sample_rate: int, channels: int, block_frames: int):
        super().__init__("udp", sample_rate, channels, block_frames)
        self.cfg = cfg

    def build_command(self) -> list[str]:
        command = self._base_command()
        command.extend(self._encoding_args(self.cfg.codec, self.cfg.bitrate, self.cfg.format))
        command.append(self._stream_url())
        return command

    def _stream_url(self) -> str:
        return f"udp://{self.cfg.host}:{self.cfg.port}?pkt_size={self.cfg.packet_size}"


class RtpSink(FFmpegSink):
    def __init__(self, cfg: OutputRtpConfig, sample_rate: int, channels: int, block_frames: int):
        super().__init__("rtp", sample_rate, channels, block_frames)
        self.cfg = cfg

    def build_command(self) -> list[str]:
        command = self._base_command()
        command.extend(self._encoding_args(self.cfg.codec, self.cfg.bitrate, self.cfg.format))
        if self.cfg.payload_type is not None:
            command.extend(["-payload_type", str(self.cfg.payload_type)])
        if self.cfg.sdp_file:
            command.extend(["-sdp_file", self.cfg.sdp_file])
        command.append(f"rtp://{self.cfg.host}:{self.cfg.port}")
        return command


class AudioEngine:
    def __init__(self, cfg: HazeConfig, block_frames: int):
        self.cfg = cfg
        self.block_frames = block_frames
        self.frame_size = block_frames * cfg.playout.channels * PCM_BYTES_PER_SAMPLE
        self.frame_duration = block_frames / cfg.playout.sample_rate
        self._audio_queue: Optional[asyncio.Queue[bytes]] = None
        self._current_track: Optional[StreamMetadata] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._pump_task: Optional[asyncio.Task[None]] = None
        self._sinks: list[AudioSink] = []

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run_loop, name="haze-audio", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("Audio engine failed to start")
        self._submit(self._async_start())

    def stop(self):
        if not self._loop:
            return
        self._submit(self._async_stop(), timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        self._loop = None
        self._audio_queue = None
        self._current_track = None

    def publish(self, pcm: bytes, timeout: float = 1.0) -> bool:
        if not self._audio_queue or not self._loop:
            return False
        packet = pcm[:self.frame_size]
        if len(packet) < self.frame_size:
            packet = packet.ljust(self.frame_size, b"\x00")
        try:
            self._submit(self._audio_queue.put(packet), timeout=timeout)
            return True
        except Exception as exc:
            log.debug("Audio queue publish failed: %s", exc)
            return False

    def buffered_frames(self) -> int:
        if not self._loop:
            return 0
        try:
            return int(self._submit(self._async_buffered_frames(), timeout=1) or 0)
        except Exception:
            return 0

    def flush(self):
        if not self._loop:
            return
        self._submit(self._async_flush())

    def set_track(self, metadata: StreamMetadata):
        self._current_track = metadata
        if not self._loop:
            return
        self._submit(self._async_set_track(metadata))

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._audio_queue = asyncio.Queue(maxsize=DEFAULT_SOURCE_QUEUE_SIZE)
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            with contextlib.suppress(Exception):
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def _submit(self, coro, timeout: Optional[float] = 5):
        if not self._loop:
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _async_start(self):
        self._sinks = self._build_sinks()
        active_sinks: list[AudioSink] = []
        for sink in self._sinks:
            try:
                await sink.start()
                if self._current_track:
                    await sink.set_track(self._current_track)
                active_sinks.append(sink)
            except Exception as exc:
                log.error("%s sink failed to start: %s", sink.name, exc)
        self._sinks = active_sinks
        if not self._sinks:
            log.warning("Audio engine started with no active sinks.")
        self._pump_task = asyncio.create_task(self._pump_loop(), name="haze-audio-pump")

    async def _async_stop(self):
        if self._pump_task:
            self._pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump_task
            self._pump_task = None
        for sink in self._sinks:
            with contextlib.suppress(Exception):
                await sink.stop()
        self._sinks.clear()
        await self._async_flush()

    async def _async_flush(self):
        if self._audio_queue:
            while True:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._audio_queue.get_nowait()
                    continue
                break
        for sink in self._sinks:
            await sink.flush()

    async def _async_set_track(self, metadata: StreamMetadata):
        for sink in self._sinks:
            with contextlib.suppress(Exception):
                await sink.set_track(metadata)

    async def _async_buffered_frames(self) -> int:
        total = self._audio_queue.qsize() if self._audio_queue else 0
        for sink in self._sinks:
            total += sink._queue.qsize()
        return total

    async def _pump_loop(self):
        silence = b"\x00" * self.frame_size
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while True:
            now = loop.time()
            if now < next_tick:
                await asyncio.sleep(next_tick - now)
            else:
                next_tick = now
            packet = silence
            if self._audio_queue:
                with contextlib.suppress(asyncio.QueueEmpty):
                    packet = self._audio_queue.get_nowait()
            for sink in self._sinks:
                await sink.enqueue(packet)
            next_tick += self.frame_duration

    def _build_sinks(self) -> list[AudioSink]:
        sinks: list[AudioSink] = []
        sample_rate = self.cfg.playout.sample_rate
        channels = self.cfg.playout.channels
        if self.cfg.soundcard.enabled:
            sinks.append(SoundcardSink(self.cfg.soundcard, sample_rate, channels, self.block_frames))
        if self.cfg.icecast.enabled:
            sinks.append(IcecastSink(self.cfg.icecast, sample_rate, channels, self.block_frames))
        if self.cfg.udp.enabled:
            sinks.append(UdpSink(self.cfg.udp, sample_rate, channels, self.block_frames))
        if self.cfg.rtp.enabled:
            sinks.append(RtpSink(self.cfg.rtp, sample_rate, channels, self.block_frames))
        return sinks