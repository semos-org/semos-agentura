"""Common settings base class for all Semos Agentura agents."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class CommonSettings(BaseSettings):
    """Base settings that all agents inherit from.

    Loads from agent-specific .env first, then falls back to workspace root .env
    for shared keys like API keys.
    """

    model_config = SettingsConfigDict(
        env_file=[".env", "../.env"],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    agent_host: str = "127.0.0.1"
    agent_port: int = 8000
