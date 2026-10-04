from __future__ import annotations

import json
import math
import os
import subprocess
from typing import Any

DEFAULT_NARRATOR_TIMEOUT = 60.0


def render_scene(brief: dict[str, Any]) -> str:
    command = os.environ.get("BREAKROOM_NARRATOR_COMMAND")
    if command:
        timeout_value = os.environ.get("BREAKROOM_NARRATOR_TIMEOUT")
        try:
            timeout = (
                DEFAULT_NARRATOR_TIMEOUT if timeout_value is None else float(timeout_value)
            )
        except ValueError as error:
            raise RuntimeError(
                f"invalid BREAKROOM_NARRATOR_TIMEOUT value: {timeout_value!r}"
            ) from error
        if not math.isfinite(timeout) or timeout <= 0:
            raise RuntimeError(
                f"invalid BREAKROOM_NARRATOR_TIMEOUT value: {timeout_value!r}; "
                "expected a positive finite number of seconds"
            )
        try:
            completed = subprocess.run(
                command,
                input=json.dumps(brief, sort_keys=True),
                capture_output=True,
                check=True,
                shell=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"narrator command timed out after {timeout:g} seconds: {command}"
            ) from error
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
