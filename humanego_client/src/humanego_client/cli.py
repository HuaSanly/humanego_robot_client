from __future__ import annotations

import argparse


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--control-mode", choices=("record_only", "execute"))
    return parser.parse_args(argv)


def apply_control_mode(config, control_mode):
    if control_mode is not None:
        config["control"]["mode"] = control_mode
    return config
