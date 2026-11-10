from __future__ import annotations

from pathlib import Path

import yaml
from lerobot.inference import RemoteInferenceConfig
from lerobot.remote_inference.client import RemoteClient


def connect_remote(config: dict, task: str):
    server = {}
    config_path = config.get("config_path")
    if config_path:
        server_path = Path(config_path).resolve()
        if server_path.exists():
            with server_path.open(encoding="utf-8") as stream:
                server = yaml.safe_load(stream) or {}
        elif not (config.get("endpoint") and config.get("deployment") and config.get("semantics")):
            raise FileNotFoundError(f"remote server config does not exist: {server_path}")
    network = config.get("endpoint") or server["zenoh"]["listen_endpoints"][0]
    deployment = config.get("deployment") or server.get("deployment")
    semantics = config.get("semantics") or server.get("semantics")
    if not deployment or not semantics:
        raise ValueError("remote deployment and semantics must be configured")
    remote_cfg = RemoteInferenceConfig(
        endpoint=network,
        deployment=deployment,
        semantics=semantics,
        chunk_merge="append",
        max_observation_age_s=float(config["max_observation_age_s"]),
        handshake_timeout_s=float(config["handshake_timeout_s"]),
        action_timeout_s=float(config["action_timeout_s"]),
        startup_timeout_s=float(config["startup_timeout_s"]),
        refill_seconds=float(config["refill_seconds"]),
        encoding=config.get("encoding", "raw"),
        jpeg_quality=int(config.get("jpeg_quality", 90)),
    )
    client = RemoteClient.connect(remote_cfg)
    caps = client.capabilities
    client.admit(features=caps.features, action_feature=caps.action_feature,
                 semantics=remote_cfg.semantics, action_interval=float(config["action_interval_s"]), mode="chunk")
    return client, remote_cfg
