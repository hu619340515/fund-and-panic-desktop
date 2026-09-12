"""读取随版本分发的固定模型配置，拒绝静默更改模型口径。"""
from pathlib import Path
import sys
import hashlib
import yaml

ROOT=Path(getattr(sys,"_MEIPASS",Path(__file__).resolve().parents[3]))
CONFIG_PATH=ROOT/"config"/"risk-model-v4.yaml"
CONFIG_SHA256=hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()
CONFIG=yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
if CONFIG.get("version")!="4.0":
    raise ValueError("风险模型配置版本不匹配")
core=CONFIG.get("core",{})
if (core.get("shock_days"),core.get("volatility_days"),core.get("drawdown_days"),core.get("reference_years")) != (5,20,60,5):
    raise ValueError("当前4.0实现仅接受已登记的5/20/60日及5年窗口，变更须升级模型版本")
if core.get("drawdown_basis") != "highest_close":
    raise ValueError("4.0回撤口径固定为最近60个真实收盘价的最高值")
if any(abs(core.get("weights",{}).get(key,0)-1/3)>1e-12 for key in ("shock","drawdown","downside")):
    raise ValueError("4.0核心权重必须为三项等权")
