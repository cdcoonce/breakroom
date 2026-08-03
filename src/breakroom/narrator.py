from __future__ import annotations

import json
import os
import subprocess
from typing import Any


def render_scene(brief: dict[str, Any]) -> str:
    command = os.environ.get("BREAKROOM_NARRATOR_COMMAND")
    if command:
        completed = subprocess.run(
            command,
            input=json.dumps(brief, sort_keys=True),
            capture_output=True,
            check=True,
            shell=True,
            text=True,
        )
        return completed.stdout.strip()

    character = brief["character"]["name"]
    incident = brief["incident"]["name"]
    return f"{character} faced {incident}."
