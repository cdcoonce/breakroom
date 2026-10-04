from __future__ import annotations

import json
import os
import subprocess
from typing import Any


def render_scene(brief: dict[str, Any]) -> str:
    command = os.environ.get("BREAKROOM_NARRATOR_COMMAND")
    if command:
        try:
            completed = subprocess.run(
                command,
                input=json.dumps(brief, sort_keys=True),
                capture_output=True,
                check=True,
                shell=True,
                text=True,
            )
        except subprocess.CalledProcessError as error:
            raise RuntimeError(
                f"narrator command failed (exit {error.returncode}): {command}\n"
                f"stdout: {error.stdout}\nstderr: {error.stderr}"
            ) from error
        output = completed.stdout.strip()
        if not output:
            raise RuntimeError(f"narrator command returned empty output: {command}")
        return output

    character = brief["character"]["name"]
    if brief["incident"] is None:
        return f"{character}: {brief['storylet']['premise']}"
    incident = brief["incident"]["name"]
    return f"{character} faced {incident}."
