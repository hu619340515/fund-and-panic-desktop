"""V3 严格配置加载。"""

from __future__ import annotations

from copy import deepcopy
from math import isfinite
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import MODEL_VERSION


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "defaults.yaml"


class Settings:
    def __init__(
        self,
        config_path: str | Path | None = None,
        runtime_paths: Mapping[str, str | Path] | None = None,
    ):
        self.config_path = (
            Path(config_path).expanduser().resolve() if config_path else None
        )
        defaults = self._load_yaml(DEFAULT_CONFIG_PATH)
        self._config = deepcopy(defaults)
        if self.config_path:
            if not self.config_path.exists():
                raise FileNotFoundError(f"配置文件不存在: {self.config_path}")
            override = self._load_yaml(self.config_path)
            self._reject_unknown(defaults, override)
            self._merge(self._config, override)
        self._runtime_paths = {
            name: Path(value).expanduser().resolve()
            for name, value in (runtime_paths or {}).items()
        }
        if self._runtime_paths:
            self._config["database"]["path"] = str(self._runtime_paths["database"])
            self._config["database"]["backup_directory"] = str(
                self._runtime_paths["backups"]
            )
            self._config["logging"]["directory"] = str(self._runtime_paths["logs"])
        self._validate()

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as stream:
            value = yaml.safe_load(stream) or {}
        if not isinstance(value, dict):
            raise ValueError(f"配置根节点必须是对象: {path}")
        return value

    @classmethod
    def _reject_unknown(
        cls,
        defaults: dict[str, Any],
        override: dict[str, Any],
        prefix: str = "",
    ) -> None:
        for key, value in override.items():
            path = f"{prefix}.{key}" if prefix else key
            if key not in defaults:
                raise ValueError(f"未知配置键: {path}")
            default_value = defaults[key]
            if isinstance(value, dict):
                if not isinstance(default_value, dict):
                    raise ValueError(f"配置类型错误: {path}")
                cls._reject_unknown(default_value, value, path)

    @classmethod
    def _merge(cls, target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                cls._merge(target[key], value)
            else:
                target[key] = deepcopy(value)

    def _validate(self) -> None:
        model = self.section("model")
        if model.get("version") != MODEL_VERSION:
            raise ValueError(f"model.version 必须是 {MODEL_VERSION}")
        power = self.number("model.generalized_mean_power")
        if power <= 0:
            raise ValueError("广义均值幂必须大于0")
        component_weights = self.section("component_weights")
        required = {"volatility", "breadth", "derivatives", "liquidity"}
        if set(component_weights) != required:
            raise ValueError("component_weights 必须且只能包含四个V3组件")
        values = [float(component_weights[key]) for key in sorted(required)]
        if any(not isfinite(value) or value < 0 for value in values):
            raise ValueError("组件权重必须是有限非负数")
        if abs(sum(values) - 1.0) > 1e-9:
            raise ValueError("组件权重之和必须等于1")
        realtime = self.section("realtime")
        if int(realtime["refresh_seconds"]) < int(realtime["minimum_refresh_seconds"]):
            raise ValueError("实时刷新间隔不得小于最低刷新间隔")
        if int(realtime["bucket_minutes"]) != 5:
            raise ValueError("V3核心桶必须为5分钟")
        reference = self.section("reference")
        thresholds = [
            int(reference["self_calibration_start_days"]),
            int(reference["same_time_history_days"]),
            int(reference["mature_history_days"]),
        ]
        if thresholds != sorted(thresholds) or thresholds[0] < 1:
            raise ValueError("参考模式历史天数配置无效")
        for section_name in ("freshness", "quality", "network"):
            for key, value in self.section(section_name).items():
                if isinstance(value, (int, float)) and (
                    not isfinite(float(value)) or float(value) < 0
                ):
                    raise ValueError(f"配置必须是有限非负数: {section_name}.{key}")
        for feature_group, weights in self.section("feature_weights").items():
            if abs(sum(float(value) for value in weights.values()) - 1.0) > 1e-9:
                raise ValueError(f"特征权重之和必须等于1: {feature_group}")
        for name, anchors in self.section("fixed_anchors").items():
            if len(anchors) < 2:
                raise ValueError(f"锚点数量不足: {name}")
            xs = [float(item[0]) for item in anchors]
            ys = [float(item[1]) for item in anchors]
            if xs != sorted(xs) or len(set(xs)) != len(xs):
                raise ValueError(f"锚点横坐标必须严格递增: {name}")
            if any(not 0 <= value <= 100 for value in ys):
                raise ValueError(f"锚点评分必须在0到100之间: {name}")

    def section(self, name: str) -> dict[str, Any]:
        value = self._config.get(name)
        if not isinstance(value, dict):
            raise KeyError(f"配置段不存在: {name}")
        return deepcopy(value)

    def get(self, path: str, default: Any = None) -> Any:
        value: Any = self._config
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                return deepcopy(default)
            value = value[part]
        return deepcopy(value)

    def number(self, path: str) -> float:
        value = float(self.get(path))
        if not isfinite(value):
            raise ValueError(f"配置数值无效: {path}")
        return value

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if path.is_absolute():
            return path.resolve()
        base = self.config_path.parent if self.config_path else PROJECT_ROOT
        return (base / path).resolve()

    def database_path(self, override: str | Path | None = None) -> Path:
        return self.resolve_path(override or self.get("database.path"))

    def backup_directory(self) -> Path:
        return self.resolve_path(self.get("database.backup_directory"))

    def cache_directory(self) -> Path:
        value = self._runtime_paths.get("cache", PROJECT_ROOT / "cache")
        return Path(value).resolve()

    def reports_directory(self) -> Path:
        value = self._runtime_paths.get("reports", PROJECT_ROOT / "reports")
        return Path(value).resolve()

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._config)
