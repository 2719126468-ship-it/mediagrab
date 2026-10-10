"""公开版测试：只依赖本地 fixture，不访问任何真实站点。"""
import sys
from pathlib import Path

# 让 tests/ 能 import 到仓库根目录的 mediagrab.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
