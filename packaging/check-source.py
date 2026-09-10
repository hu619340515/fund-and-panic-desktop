"""发布前检查已跟踪源码，不输出任何疑似密钥内容。"""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
problems: list[str] = []
secrets = [
    re.compile(rb"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
]
for name in filter(None, paths):
    path = ROOT / name
    if not path.exists():
        problems.append(f"待提交文件不存在：{name}")
        continue
    if any(part in {"__pycache__", "node_modules", "dist", "build", "logs", "cache", "data_cache", "reports"} for part in path.relative_to(ROOT).parts):
        problems.append(f"生成目录不应提交：{name}")
    if path.suffix.lower() in {".db", ".sqlite", ".pyc", ".exe", ".zip", ".skill"} or path.name == "SKILL.md":
        problems.append(f"发布排除文件：{name}")
    content = path.read_bytes()
    if any(pattern.search(content) for pattern in secrets):
        problems.append(f"疑似密钥：{name}")
    if path.suffix.lower() in {".py", ".ts", ".json", ".md", ".yaml", ".yml", ".css", ".html", ".ps1", ".cjs", ".txt", ".toml", ".csv"}:
        if content.startswith(b"\xef\xbb\xbf"):
            problems.append(f"文本含 BOM：{name}")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"文本不是 UTF-8：{name}")
            continue
        if "her" + "mes" in text.lower():
            problems.append(f"旧技能内容：{name}")
if problems:
    raise SystemExit("\n".join(problems))
print(f"源码检查通过，共 {len(list(filter(None, paths)))} 个文件；未发现密钥、缓存、数据库、旧技能或编码问题。")
