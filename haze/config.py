from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml



@dataclass
class OutputSoundcardConfig:
    enabled: bool = True
    device: Optional[str] = None


@dataclass
class OutputIcecastConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    mount: str = "/haze"
    username: str = "source"
    password: str = "hackme"
    codec: str = "libmp3lame"
    bitrate: str = "192k"
    format: str = "mp3"
    content_type: str = "audio/mpeg"
    name: str = "Haze"
    description: str = ""
    genre: str = ""
    public: bool = False
    metadata_enabled: bool = True
    admin_username: Optional[str] = None
    admin_password: Optional[str] = None


@dataclass
class OutputUdpConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 5004
    codec: str = "aac"
    bitrate: str = "192k"
    format: str = "mpegts"
    packet_size: int = 1316


@dataclass
class OutputRtpConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 5006
    codec: str = "opus"
    bitrate: str = "128k"
    format: str = "rtp"
    payload_type: Optional[int] = None
    sdp_file: Optional[str] = None


@dataclass
class TransitionsConfig:
    default: str = "finish_track"
    crossfade_duration: float = 2.0


@dataclass
class PlayoutConfig:
    sample_rate: int = 48000
    channels: int = 2
    default_playlist: Optional[str] = None
    shuffle: bool = False
    shuffle_carry_over: int = 3


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class HazeConfig:
    playout: PlayoutConfig = field(default_factory=PlayoutConfig)
    soundcard: OutputSoundcardConfig = field(default_factory=OutputSoundcardConfig)
    icecast: OutputIcecastConfig = field(default_factory=OutputIcecastConfig)
    udp: OutputUdpConfig = field(default_factory=OutputUdpConfig)
    rtp: OutputRtpConfig = field(default_factory=OutputRtpConfig)
    transitions: TransitionsConfig = field(default_factory=TransitionsConfig)
    web: WebConfig = field(default_factory=WebConfig)
    playlists_dir: Path = Path("Managed/Playlists")


def load(path: Path = Path("config.yaml")) -> HazeConfig:
    raw: dict = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    playout_raw = raw.get("playout", {})
    outputs_raw = raw.get("outputs", {})
    sc_raw = outputs_raw.get("soundcard", {})
    icecast_raw = outputs_raw.get("icecast", {})
    udp_raw = outputs_raw.get("udp", {})
    rtp_raw = outputs_raw.get("rtp", {})
    trans_raw = raw.get("transitions", {})
    web_raw = raw.get("web", {})
    paths_raw = raw.get("paths", {})

    return HazeConfig(
        playout=PlayoutConfig(
            sample_rate=playout_raw.get("sample_rate", 48000),
            channels=playout_raw.get("channels", 2),
            default_playlist=playout_raw.get("default_playlist"),
            shuffle=playout_raw.get("shuffle", False),
            shuffle_carry_over=playout_raw.get("shuffle_carry_over", 3),
        ),
        soundcard=OutputSoundcardConfig(
            enabled=sc_raw.get("enabled", True),
            device=sc_raw.get("device"),
        ),
        icecast=OutputIcecastConfig(
            enabled=icecast_raw.get("enabled", False),
            host=icecast_raw.get("host", "127.0.0.1"),
            port=icecast_raw.get("port", 8000),
            mount=icecast_raw.get("mount", "/haze"),
            username=icecast_raw.get("username", "source"),
            password=icecast_raw.get("password", "hackme"),
            codec=icecast_raw.get("codec", "libmp3lame"),
            bitrate=icecast_raw.get("bitrate", "192k"),
            format=icecast_raw.get("format", "mp3"),
            content_type=icecast_raw.get("content_type", "audio/mpeg"),
            name=icecast_raw.get("name", "Haze"),
            description=icecast_raw.get("description", ""),
            genre=icecast_raw.get("genre", ""),
            public=icecast_raw.get("public", False),
            metadata_enabled=icecast_raw.get("metadata_enabled", True),
            admin_username=icecast_raw.get("admin_username"),
            admin_password=icecast_raw.get("admin_password"),
        ),
        udp=OutputUdpConfig(
            enabled=udp_raw.get("enabled", False),
            host=udp_raw.get("host", "127.0.0.1"),
            port=udp_raw.get("port", 5004),
            codec=udp_raw.get("codec", "aac"),
            bitrate=udp_raw.get("bitrate", "192k"),
            format=udp_raw.get("format", "mpegts"),
            packet_size=udp_raw.get("packet_size", 1316),
        ),
        rtp=OutputRtpConfig(
            enabled=rtp_raw.get("enabled", False),
            host=rtp_raw.get("host", "127.0.0.1"),
            port=rtp_raw.get("port", 5006),
            codec=rtp_raw.get("codec", "opus"),
            bitrate=rtp_raw.get("bitrate", "128k"),
            format=rtp_raw.get("format", "rtp"),
            payload_type=rtp_raw.get("payload_type"),
            sdp_file=rtp_raw.get("sdp_file"),
        ),
        transitions=TransitionsConfig(
            default=trans_raw.get("default", "finish_track"),
            crossfade_duration=trans_raw.get("crossfade_duration", 2.0),
        ),
        web=WebConfig(
            enabled=web_raw.get("enabled", True),
            host=web_raw.get("host", "0.0.0.0"),
            port=web_raw.get("port", 8080),
        ),
        playlists_dir=Path(paths_raw.get("playlists_dir", "Managed/Playlists")),
    )