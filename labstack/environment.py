"""Resolve dotenv files once and render literal guest environment values."""

import shlex
from io import StringIO
from pathlib import Path

from dotenv.parser import parse_stream
from pydantic import TypeAdapter, ValidationError

from labstack.models.config import Config
from labstack.models.environment import Environment
from labstack.settings import get_settings


def read_environment_file(filename: str, directory: Path) -> dict[str, str]:
    """Read a dotenv file inside the inventory directory without shell expansion.

    Parsing never evaluates commands or substitutes host variables. Errors name the
    file and line, but omit its contents because a value may contain a credential.
    Resolving links before opening the file keeps reads within the inventory folder.
    """
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError(f"Unavailable environment file: {filename}.")
    try:
        with path.open(encoding="utf-8-sig") as stream:
            contents = stream.read(1024**2 + 1)
    except OSError, UnicodeError:
        raise ValueError(f"Cannot read environment file: {filename}.") from None
    if len(contents) > 1024**2:
        raise ValueError(f"Environment file exceeds 1 MiB: {filename}.")
    # Use the parser directly: the higher-level loader logs malformed input and can
    # interpolate variables. Neither behavior is suitable for private guest values.
    values: dict[str, str] = {}
    validator = TypeAdapter(Environment)
    for binding in parse_stream(StringIO(contents)):
        if binding.error:
            raise ValueError(
                f"Invalid environment file {filename}, line {binding.original.line}."
            )
        if binding.key is None:
            continue
        try:
            values.update(validator.validate_python({binding.key: binding.value}))
        except ValidationError:
            raise ValueError(
                f"Invalid environment file {filename}, line {binding.original.line}."
            ) from None
    return values


def resolve_environments(
    config: Config, directory: Path | None = None
) -> dict[str, dict[str, str]]:
    """Snapshot active machines' values before creating any VM resources.

    Files are read in order, then explicit YAML values win. The snapshot is saved
    separately from the inventory so restart and reset do not reread changed files.
    Inactive profile files are not needed until that profile is selected.
    """
    directory = directory or get_settings().root
    environments = {}
    for name, machine in config.active_machines.items():
        values = {}
        for filename in machine.env_file:
            values.update(read_environment_file(filename, directory))
        values.update(machine.environment)
        environments[name] = values
    return environments


def shell_environment(values: dict[str, str]) -> str:
    """Export literal values for Bash, including quotes, dollars and backticks.

    This text is sent through SSH stdin or a private guest file. Keeping values out
    of the SSH command also keeps credentials out of its host process arguments.
    """
    return "".join(
        f"export {key}={shlex.quote(value)}\n" for key, value in values.items()
    )


def systemd_environment(values: dict[str, str]) -> str:
    """Quote values for EnvironmentFile, whose format differs from shell exports.

    Systemd does not expand dollar signs or execute backticks here. Backslashes
    and double quotes still need escaping inside its double-quoted values.
    """
    lines = []
    for key, value in values.items():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"\n')
    return "".join(lines)
