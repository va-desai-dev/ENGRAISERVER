from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


APP_ID = "engrai-server"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Mutable application locations, independent of the installed package."""

    config: Path
    data: Path
    state: Path
    cache: Path
    environment: Path
    portable: bool


def _xdg(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable, "").strip()
    return Path(value).expanduser() if value else fallback


def application_paths() -> AppPaths:
    """Return the XDG layout, or an explicit portable root.

    ``ENGRAI_HOME`` is useful for development checkouts and removable disks.
    Without it, mutable files never land beside package code in site-packages
    and never depend on the current directory.
    """

    override = os.environ.get("ENGRAI_HOME", "").strip()
    if override:
        root = Path(override).expanduser().resolve()
        return AppPaths(
            config=root,
            data=root,
            state=root / "state",
            cache=root / "cache",
            environment=root / ".env",
            portable=True,
        )

    home = Path.home()
    config = _xdg("XDG_CONFIG_HOME", home / ".config") / APP_ID
    data = _xdg("XDG_DATA_HOME", home / ".local" / "share") / APP_ID
    state = _xdg("XDG_STATE_HOME", home / ".local" / "state") / APP_ID
    cache = _xdg("XDG_CACHE_HOME", home / ".cache") / APP_ID
    return AppPaths(
        config=config,
        data=data,
        state=state,
        cache=cache,
        environment=config / "gateway.env",
        portable=False,
    )


def app_home() -> Path:
    """Compatibility name for the active installation's data root."""

    return application_paths().data


APP_HOME = app_home()


def environment_files() -> tuple[Path, ...]:
    """Configuration files accepted by the installed CLI."""

    return (application_paths().environment,)


def _config_path(name: str) -> Path:
    return application_paths().config / name


def _data_path(name: str) -> Path:
    return application_paths().data / name


def _state_path(name: str) -> Path:
    return application_paths().state / name


def default_hf_cache() -> Path:
    """Resolve the Hugging Face hub cache the way the CLI itself does.

    The layout under the cache root is a stable convention, but the root is
    not: it moves whenever GGUF weights outgrow the system drive. Mirroring
    huggingface_hub's precedence keeps model discovery pointed at wherever
    `hf download` actually writes. huggingface_hub is not a dependency here,
    so the six lines are reimplemented rather than imported.
    """
    if cache := os.environ.get("HF_HUB_CACHE", "").strip():
        return Path(cache).expanduser()
    if hf_home := os.environ.get("HF_HOME", "").strip():
        return Path(hf_home).expanduser() / "hub"
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "huggingface" / "hub"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Where the gateway itself listens. Loopback by default: a fresh install
    # must not expose an admin surface to the LAN before its owner asks for it.
    # Set 0.0.0.0 to serve every interface, or a specific address to pin one.
    bind_host: str = Field("127.0.0.1", validation_alias=AliasChoices("ENGRAI_BIND_HOST", "BIND_HOST"))
    bind_port: int = Field(8400, validation_alias=AliasChoices("ENGRAI_BIND_PORT", "BIND_PORT"))
    # Comma-separated peers whose X-Forwarded-* headers are trusted, or "*".
    # Only meaningful behind a reverse proxy such as cloudflared.
    forwarded_allow_ips: str = Field(
        "127.0.0.1",
        validation_alias=AliasChoices("ENGRAI_FORWARDED_ALLOW_IPS", "FORWARDED_ALLOW_IPS"),
    )

    # Seed values only. They populate the XDG credential store on first run and
    # are ignored from then on, since keys and the password become editable in
    # the web UI. The store is the source of truth once it exists.
    control_api_keys: str = Field("", validation_alias=AliasChoices("ENGRAI_API_KEYS", "CONTROL_API_KEYS"))
    control_admin_token: str = Field("", validation_alias=AliasChoices("ENGRAI_ADMIN_TOKEN", "CONTROL_ADMIN_TOKEN"))
    credentials_path: Path = Field(
        default_factory=lambda: _state_path("credentials.json"),
        validation_alias=AliasChoices("ENGRAI_CREDENTIALS_PATH", "CREDENTIALS_PATH"),
    )
    preferences_path: Path = Field(
        default_factory=lambda: _state_path("preferences.json"),
        validation_alias=AliasChoices("ENGRAI_PREFERENCES_PATH", "PREFERENCES_PATH"),
    )
    deployment_dir: Path = Field(
        default_factory=lambda: _config_path("deployments"),
        validation_alias=AliasChoices("ENGRAI_DEPLOYMENT_DIR", "DEPLOYMENT_DIR"),
    )
    runtime_dir: Path = Field(
        default_factory=lambda: _data_path("runtimes"),
        validation_alias=AliasChoices("ENGRAI_RUNTIME_DIR", "RUNTIME_DIR"),
    )
    llamacpp_executable: Path = Field(
        default_factory=lambda: _data_path("bin/llama-server"),
        validation_alias=AliasChoices("ENGRAI_LLAMACPP_EXECUTABLE", "LLAMACPP_EXECUTABLE"),
    )
    engine_host: str = Field(
        "127.0.0.1", validation_alias=AliasChoices("ENGRAI_ENGINE_HOST", "ENGINE_HOST")
    )
    text_engine_port: int = Field(
        5002, validation_alias=AliasChoices("ENGRAI_TEXT_ENGINE_PORT", "TEXT_ENGINE_PORT")
    )
    route_dir: Path = Field(
        default_factory=lambda: _config_path("routes"),
        validation_alias=AliasChoices("ENGRAI_ROUTE_DIR", "ROUTE_DIR"),
    )
    text_engine_log_path: Path = Field(
        default_factory=lambda: _state_path("text-worker.log"),
        validation_alias=AliasChoices("ENGRAI_TEXT_ENGINE_LOG", "TEXT_ENGINE_LOG"),
    )
    # Image generation is deliberately a second loopback-only worker. A large
    # image model can then live on a spare GPU without restarting the LLM, its
    # KV cache, or the chat request lane.
    image_engine_port: int = Field(
        5003, validation_alias=AliasChoices("ENGRAI_IMAGE_ENGINE_PORT", "IMAGE_ENGINE_PORT")
    )
    image_command_dir: Path = Field(
        default_factory=lambda: _config_path("image-commands"),
        validation_alias=AliasChoices("ENGRAI_IMAGE_COMMAND_DIR", "IMAGE_COMMAND_DIR"),
    )
    image_engine_log_path: Path = Field(
        default_factory=lambda: _state_path("image-worker.log"),
        validation_alias=AliasChoices("ENGRAI_IMAGE_ENGINE_LOG", "IMAGE_ENGINE_LOG"),
    )
    image_state_path: Path = Field(default_factory=lambda: _state_path("image-runtime.json"))
    image_result_dir: Path = Field(default_factory=lambda: _state_path("image-results"))
    image_result_ttl_seconds: int = Field(
        3600, validation_alias=AliasChoices("ENGRAI_IMAGE_RESULT_TTL_SECONDS", "IMAGE_RESULT_TTL_SECONDS")
    )
    engine_stop_timeout_seconds: float = Field(
        25.0, validation_alias=AliasChoices("ENGRAI_ENGINE_STOP_TIMEOUT_SECONDS", "ENGINE_STOP_TIMEOUT_SECONDS")
    )
    model_search_roots: str = Field(
        default_factory=lambda: str(default_hf_cache()),
        validation_alias=AliasChoices("ENGRAI_MODEL_SEARCH_ROOTS", "MODEL_SEARCH_ROOTS"),
    )
    model_download_dir: Path = Field(
        default_factory=default_hf_cache,
        validation_alias=AliasChoices("ENGRAI_MODEL_DOWNLOAD_DIR", "MODEL_DOWNLOAD_DIR"),
    )
    model_download_state_path: Path = Field(
        default_factory=lambda: _state_path("model-downloads.json"),
        validation_alias=AliasChoices(
            "ENGRAI_MODEL_DOWNLOAD_STATE_PATH", "MODEL_DOWNLOAD_STATE_PATH"
        ),
    )
    model_state_path: Path = Field(
        default_factory=lambda: _state_path("runtime.json"),
        validation_alias=AliasChoices("ENGRAI_MODEL_STATE_PATH", "MODEL_STATE_PATH"),
    )
    model_load_timeout_seconds: float = Field(
        240.0, validation_alias=AliasChoices("ENGRAI_MODEL_LOAD_TIMEOUT_SECONDS", "MODEL_LOAD_TIMEOUT_SECONDS")
    )

    @field_validator(
        "llamacpp_executable",
        "route_dir",
        "text_engine_log_path",
        "image_command_dir",
        "image_engine_log_path",
        "model_state_path",
        "image_state_path",
        "image_result_dir",
        "credentials_path",
        "preferences_path",
        "deployment_dir",
        "runtime_dir",
        "model_download_dir",
        "model_download_state_path",
        mode="after",
    )
    @classmethod
    def anchor_to_application_layout(cls, value: Path, info: ValidationInfo) -> Path:
        value = value.expanduser()
        if value.is_absolute():
            return value
        paths = application_paths()
        if paths.portable:
            return paths.data / value
        if info.field_name in {"deployment_dir", "route_dir", "image_command_dir"}:
            base = paths.config
        elif info.field_name in {"llamacpp_executable", "runtime_dir", "model_download_dir"}:
            base = paths.data
        else:
            base = paths.state
        return base / value

    @property
    def api_keys(self) -> list[str]:
        return [item.strip() for item in self.control_api_keys.split(",") if item.strip()]

    @property
    def search_roots(self) -> list[Path]:
        return [
            Path(item.strip()).expanduser()
            for item in self.model_search_roots.split(os.pathsep)
            if item.strip()
        ]

    @field_validator("control_admin_token")
    @classmethod
    def strip_admin_token(cls, value: str) -> str:
        return value.strip()

    @property
    def forwarded_peers(self) -> list[str] | str:
        value = self.forwarded_allow_ips.strip()
        if value == "*":
            return value
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def text_engine_base_url(self) -> str:
        return f"http://{self.engine_host}:{self.text_engine_port}"

    @property
    def image_engine_base_url(self) -> str:
        return f"http://{self.engine_host}:{self.image_engine_port}"

    @model_validator(mode="after")
    def derive_image_worker_defaults(self) -> "Settings":
        """Keep temporary/test installations isolated without extra knobs.

        If a caller relocates the text worker's port or state paths but does
        not mention image settings, its image worker follows beside them.
        Explicit image settings always win.
        """
        supplied = self.model_fields_set
        if "deployment_dir" not in supplied and "route_dir" in supplied:
            self.deployment_dir = self.route_dir.parent / "deployments"
        if "image_engine_port" not in supplied and "text_engine_port" in supplied:
            self.image_engine_port = self.text_engine_port + 1
        if "image_command_dir" not in supplied and "route_dir" in supplied:
            self.image_command_dir = self.route_dir.parent / "image-commands"
        if "image_engine_log_path" not in supplied and "text_engine_log_path" in supplied:
            self.image_engine_log_path = self.text_engine_log_path.parent / "image-worker.log"
        if "image_state_path" not in supplied and "model_state_path" in supplied:
            self.image_state_path = self.model_state_path.parent / "image-runtime.json"
        if "image_result_dir" not in supplied and "model_state_path" in supplied:
            self.image_result_dir = self.model_state_path.parent / "image-results"
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=environment_files())  # type: ignore[call-arg]
