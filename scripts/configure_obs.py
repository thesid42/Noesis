"""Configure the project's isolated portable OBS profile (no desktop/mic capture)."""

from __future__ import annotations

import json
from pathlib import Path
import secrets

from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    config = ROOT / ".runtime" / "obs" / "config" / "obs-studio"
    config.mkdir(parents=True, exist_ok=True)
    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    password = dotenv_values(env_path).get("OBS_PASSWORD")
    if not password:
        password = secrets.token_urlsafe(24)
        set_key(str(env_path), "OBS_PASSWORD", password)
    socket_config = config / "plugin_config" / "obs-websocket" / "config.json"
    socket_config.parent.mkdir(parents=True, exist_ok=True)
    socket_config.write_text(json.dumps({
        "first_load": False,
        "server_enabled": True,
        "server_port": 4455,
        "alerts_enabled": False,
        "auth_required": True,
        "server_password": password,
    }, indent=2), encoding="utf-8")
    # In OBS this flag means first-run setup has already been completed. The
    # generated profile is complete; opening the wizard disables recording.
    initial = "[General]\nFirstRun=true\nLicenseAccepted=true\n\n[Basic]\nProfile=Noesis\nProfileDir=Noesis\nSceneCollection=Noesis\nSceneCollectionFile=Noesis\n\n[BasicWindow]\nWarnBeforeStartingStream=true\n"
    for name in ("global.ini", "user.ini"):
        path = config / name
        if not path.exists():
            path.write_text(initial, encoding="utf-8")
    profile = config / "basic" / "profiles" / "Noesis" / "basic.ini"
    profile.parent.mkdir(parents=True, exist_ok=True)
    recordings = ROOT / "recordings"
    recordings.mkdir(exist_ok=True)
    if not profile.exists():
        profile.write_text(
            "[General]\nName=Noesis\n\n[Video]\nBaseCX=1280\nBaseCY=720\nOutputCX=1280\nOutputCY=720\nFPSType=0\nFPSCommon=30\n\n"
            "[Output]\nMode=Simple\n\n[SimpleOutput]\nRecFormat2=mkv\nRecQuality=Small\nRecEncoder=x264\n"
            f"FilePath={recordings.as_posix()}\n\n[Audio]\nSampleRate=48000\nChannelSetup=Stereo\n",
            encoding="utf-8",
        )
    scenes = config / "basic" / "scenes" / "Noesis.json"
    scenes.parent.mkdir(parents=True, exist_ok=True)
    if not scenes.exists():
        scenes.write_text(json.dumps({
            "name": "Noesis", "current_scene": "NOESIS_Program", "current_program_scene": "NOESIS_Program",
            "scene_order": [{"name": "NOESIS_Program"}],
            "sources": [{"name": "NOESIS_Program", "id": "scene", "settings": {"items": []}, "mixers": 0}],
            "groups": [], "transitions": [],
            "DesktopAudioDevice1": None, "DesktopAudioDevice2": None,
            "AuxAudioDevice1": None, "AuxAudioDevice2": None, "AuxAudioDevice3": None,
        }, indent=2), encoding="utf-8")
    print("Portable OBS profile configured; private WebSocket password stored in .env.")


if __name__ == "__main__":
    main()
