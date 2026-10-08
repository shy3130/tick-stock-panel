"""tsp.spec 打包不变量 — 运行时动态发现的模块必须随冻结产物发布 (issue #459)。

app/extensions/loader.py 用 importlib.import_module + pkgutil.iter_modules 在
运行时发现 app/custom/*, 两条链路对 PyInstaller 静态分析都不可见; tsp.spec
未声明时冻结产物里扩展静默消失(桌面版 AI 对话助手 405, 且无任何日志)。
锁定双声明契约: 
  - hiddenimports(collect_submodules) 进 PYZ —— 保 import_module 可导入; 
  - datas 落盘到 app/custom —— frozen 下 app.__path__ 解析到 _internal/app, 
    保 pkgutil.iter_modules 可枚举(与 fuyao 插件同一处理方式)。
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_TEXT = (REPO_ROOT / "packaging" / "tsp.spec").read_text(encoding="utf-8")
# 只断言代码行, 避免被注释里的字样误判为已声明
SPEC_CODE = "\n".join(
    line for line in SPEC_TEXT.splitlines() if not line.lstrip().startswith("#")
)


def test_spec_declares_custom_extensions():
    """app/custom/* 双声明: PYZ 可导入 + 磁盘可枚举, 缺一冻结后扩展静默消失。"""
    assert 'collect_submodules("app.custom")' in SPEC_CODE
    assert '"app/custom"' in SPEC_CODE


def test_spec_declares_builtin_plugin_fuyao():
    """fuyao 插件双声明不被回归 (同类动态发现问题, 见 spec 内注释)。"""
    assert "app.plugins.fuyao.provider" in SPEC_CODE
    assert "app/plugins/fuyao" in SPEC_CODE
