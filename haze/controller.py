from __future__ import annotations

import logging
import subprocess
import threading
import time
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from .audio import AudioEngine, StreamMetadata
from .config import HazeConfig
from .metadata import TrackMetadata, read as read_metadata
from .playlist import Playlist, Track, discover
from .shuffle import ShuffleDeck

if TYPE_CHECKING:
    from .webserver import WebServer

log = logging.getLogger(__name__)

CHUNK_FRAMES = 2048

class State(Enum):
    STOPPED = auto()
    PLAYING = auto()
    PAUSED = auto()

class Controller:
    def __init__(self, cfg: HazeConfig):
        self.cfg = cfg
        self.state = State.STOPPED

        self.playlists: dict[str, Playlist] = {}
        self.active_playlist: Optional[Playlist] = None
        self._pending_playlist: Optional[Playlist] = None
        self._track_index: int = 0
        self._shuffle: bool = cfg.playout.shuffle
        self._deck: Optional[ShuffleDeck] = None
        self.current_meta: TrackMetadata = TrackMetadata()
        
        self.elapsed_seconds: float = 0.0

        self._audio_engine = AudioEngine(cfg, CHUNK_FRAMES)
        self._stop_decode = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()

        self._decode_thread: Optional[threading.Thread] = None

        self._webserver: Optional[WebServer] = None
        self._tui: Optional[object] = None

        self._lock = threading.Lock()

    @property
    def current_track(self) -> Optional[Track]:
        if not self.active_playlist or not self.active_playlist.tracks:
            return None
        return self.active_playlist.tracks[self._current_index()]

    def set_webserver(self, ws: WebServer):
        self._webserver = ws

    def set_tui(self, tui: object):
        self._tui = tui

    def load_playlists(self):
        self.playlists = discover(self.cfg)
        log.info(f"Discovered {len(self.playlists)} playlist(s): {list(self.playlists.keys())}")

    def start(self):
        self._audio_engine.start()
        self.state = State.STOPPED
        default = self.cfg.playout.default_playlist
        if default and default in self.playlists:
            self._activate(self.playlists[default])
        elif self.playlists:
            self._activate(next(iter(self.playlists.values())))

    def stop(self):
        self._stop_decode.set()
        self._pause_event.set()
        if self._decode_thread and self._decode_thread.is_alive():
            self._decode_thread.join(timeout=1)
        self._audio_engine.stop()
        self.state = State.STOPPED

    def pause(self):
        if self.state == State.PLAYING:
            self._pause_event.clear()
            self.state = State.PAUSED
            if self._webserver:
                self._webserver.broadcast_state_change()

    def resume(self):
        if self.state == State.PAUSED:
            self._pause_event.set()
            self.state = State.PLAYING
            if self._webserver:
                self._webserver.broadcast_state_change()

    def next_track(self):
        with self._lock:
            self._advance()
            self._play_current()

    def prev_track(self):
        with self._lock:
            self._rewind()
            self._play_current()

    def toggle_shuffle(self):
        self._shuffle = not self._shuffle
        self._rebuild_deck()
        log.info(f"Shuffle {'enabled' if self._shuffle else 'disabled'}")
        if self._webserver:
            self._webserver.broadcast_state_change()

    def switch_to(self, name: str, immediate: bool = False):
        if name not in self.playlists:
            return
        pl = self.playlists[name]
        transition = pl.transition or self.cfg.transitions.default

        with self._lock:
            if immediate or transition == "immediate" or self.active_playlist is None:
                self._activate(pl)
            else:
                self._pending_playlist = pl
                log.info(f"Queued switch to '{name}' ({transition})")
                if self._webserver:
                    self._webserver.broadcast_state_change()

    def reload_playlists(self):
        current_name = self.active_playlist.name if self.active_playlist else None
        self.load_playlists()
        if not current_name or current_name not in self.playlists:
            if self.playlists:
                self._activate(next(iter(self.playlists.values())))
        if self._webserver:
            self._webserver.broadcast_state_change()

    def _current_index(self) -> int:
        if not self.active_playlist:
            return 0
        if self._shuffle and self._deck:
            return self._deck.current()
        return self._track_index % len(self.active_playlist.tracks)

    def _activate(self, pl: Playlist):
        self.active_playlist = pl
        self._track_index = 0
        self._rebuild_deck()
        self._play_current()

    def _rebuild_deck(self):
        if not self.active_playlist:
            return
        n = len(self.active_playlist.tracks)
        if self._shuffle:
            self._deck = ShuffleDeck(n, carry_over=self.cfg.playout.shuffle_carry_over)
        else:
            self._deck = None

    def _advance(self):
        if not self.active_playlist:
            return
        if self._shuffle and self._deck:
            self._deck.advance()
        else:
            self._track_index = (self._track_index + 1) % len(self.active_playlist.tracks)

    def _rewind(self):
        if not self.active_playlist:
            return
        if self._shuffle and self._deck:
            self._deck.rewind()
        else:
            self._track_index = (self._track_index - 1) % len(self.active_playlist.tracks)

    def _play_current(self):
        """Kills existing decoder and starts a new one for the current track."""
        if not self.active_playlist or not self.active_playlist.tracks:
            return

        self._stop_decode.set()
        self._pause_event.set()
        if self._decode_thread and self._decode_thread.is_alive():
            if threading.current_thread() is not self._decode_thread:
                self._decode_thread.join(timeout=1.0)

        self._audio_engine.flush()

        track = self.current_track
        if not track:
            return

        self.current_meta = read_metadata(track.path)
        if self.current_meta.title: track.title = self.current_meta.title
        if self.current_meta.duration: track.duration = self.current_meta.duration
        self.current_meta.save_art()
        self._write_now_playing(track)
        self._audio_engine.set_track(StreamMetadata.from_track(track, self.current_meta))


        log.info(f"Playing: {track}")
        self.elapsed_seconds = 0.0

        if self._webserver:
            self._webserver.broadcast_track_change()

        if self._tui and hasattr(self._tui, "notify_track_start"):
            self._tui.notify_track_start()

        self._stop_decode.clear()
        self.state = State.PLAYING

        self._decode_thread = threading.Thread(
            target=self._decode_loop,
            args=(track.path,),
            daemon=True,
        )
        self._decode_thread.start()

    def _decode_loop(self, path: Path):
        bytes_per_sample = self.cfg.playout.channels * 2
        chunk_size = CHUNK_FRAMES * bytes_per_sample
        frame_duration = CHUNK_FRAMES / self.cfg.playout.sample_rate

        proc = subprocess.Popen(
            [
                "ffmpeg", "-loglevel", "error",
                "-probesize", "32",
                "-analyzeduration", "0",
                "-i", str(path),
                "-f", "s16le",
                "-ar", str(self.cfg.playout.sample_rate),
                "-ac", str(self.cfg.playout.channels),
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        try:
            while not self._stop_decode.is_set():
                self._pause_event.wait()
                if self._stop_decode.is_set():
                    break
                
                chunk = proc.stdout.read(chunk_size)
                if not chunk:
                    break
                
                try:
                    if self._audio_engine.publish(chunk, timeout=0.5):
                        self.elapsed_seconds += frame_duration
                except Exception:
                    continue

            if not self._stop_decode.is_set():
                while self._audio_engine.buffered_frames() > 0 and not self._stop_decode.is_set():
                    time.sleep(0.05)

        finally:
            proc.kill()
            proc.wait()

        if not self._stop_decode.is_set():
            self._on_track_end()

    def _on_track_end(self):
        threading.Thread(target=self._next_internal, daemon=True).start()

    def _next_internal(self):
        with self._lock:
            if self._pending_playlist is not None:
                pl = self._pending_playlist
                self._pending_playlist = None
                self._activate(pl)
            else:
                self._advance()
                self._play_current()

    def _write_now_playing(self, track: Track):
        try:
            meta = self.current_meta
            lines = [f"title={meta.title or track.path.stem}", f"artist={meta.artist or ''}", f"timestamp={datetime.now().isoformat()}"]
            Path("now_playing.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception: pass