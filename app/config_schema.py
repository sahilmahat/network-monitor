"""
config_schema.py — Pydantic models that validate config.yaml at startup.

If the config is malformed, the app crashes immediately with a clear error
message instead of silently misbehaving.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Literal
import ipaddress
import yaml
from pathlib import Path


class Target(BaseModel):
    """A single monitored target (gateway or VM)."""
    name: str = Field(..., min_length=1, max_length=100)
    host: str = Field(..., min_length=1)
    type: Literal["gateway", "vm"]

    @field_validator("host")
    @classmethod
    def host_must_be_ip_or_hostname(cls, v: str) -> str:
        """Allow IPv4/IPv6 addresses OR simple hostnames."""
        v = v.strip()
        # Try IP first
        try:
            ipaddress.ip_address(v)
            return v
        except ValueError:
            pass
        # Fall back to basic hostname check (alphanumeric + . + -)
        cleaned = v.replace(".", "").replace("-", "")
        if not cleaned.isalnum():
            raise ValueError(f"'{v}' is not a valid IP address or hostname")
        return v


class Config(BaseModel):
    """Top-level config.yaml structure."""
    ping_interval_seconds: int = Field(default=30, ge=5, le=3600)
    ping_timeout_seconds: int = Field(default=2, ge=1, le=30)
    retention_days: int = Field(default=30, ge=1, le=365)
    gateways: list[Target] = Field(default_factory=list)
    vms: list[Target] = Field(default_factory=list)

    @field_validator("gateways", "vms")
    @classmethod
    def must_be_correct_type(cls, v: list[Target], info) -> list[Target]:
        """Ensure gateways list contains type='gateway', vms list contains type='vm'."""
        expected = "gateway" if info.field_name == "gateways" else "vm"
        for target in v:
            if target.type != expected:
                raise ValueError(
                    f"Target '{target.name}' in '{info.field_name}' has type='{target.type}', expected '{expected}'"
                )
        return v


def load_and_validate_config(path: str = "config.yaml") -> Config:
    """
    Load config.yaml, validate it with Pydantic, return a validated Config object.
    Raises a clear error if the file is malformed.
    """
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path.resolve()}")

    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raise ValueError(f"{config_path} is empty")

    return Config(**raw)


# Standalone test runner — `python -m app.config_schema`
if __name__ == "__main__":
    try:
        cfg = load_and_validate_config()
        print("✅ Config is valid")
        print(f"   Interval: {cfg.ping_interval_seconds}s")
        print(f"   Timeout:  {cfg.ping_timeout_seconds}s")
        print(f"   Retention:{cfg.retention_days}d")
        print(f"   Gateways: {len(cfg.gateways)}")
        print(f"   VMs:      {len(cfg.vms)}")
    except Exception as e:
        print(f"❌ Config validation failed:\n{e}")
        exit(1)
