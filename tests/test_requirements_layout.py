"""依赖清单的分层约束。

分层原因：Streamlit Community Cloud 只读取仓库根目录的 `requirements.txt`，
且免费档资源上限约 1GB。若把 `sentence-transformers`（连带 torch，约 2GB）
放进去，公网 Demo 会直接构建失败。因此：

- `requirements.txt`      最小运行集合，CI 与公网 Demo 都装它；
- `requirements-rag.txt`  可选本地向量栈，只有需要语义检索时才装；
- `requirements-lock.txt` 桌面完整版的精确锁定版本。

本文件把上述约束变成可执行断言，避免有人「顺手」把 torch 加回根清单后
在部署时才发现问题。
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 任何会直接或间接带入 torch / 大型模型运行时的包，都不允许出现在根清单里。
HEAVY_PACKAGES = {"chromadb", "sentence-transformers", "torch", "transformers"}

# 必须只由可选清单声明的包。
OPTIONAL_RAG_PACKAGES = {"chromadb", "sentence-transformers"}

# 公网 Demo 与 CI 在缺少向量栈时仍必须可导入的模块。
MODULES_THAT_MUST_IMPORT_WITHOUT_VECTOR_STACK = (
    "app",
    "ui_public_demo",
    "public_demo_store",
    "runtime_config",
    "deployment_check",
    "rag.rag_service",
    "rag.vector_store",
    "rag.embeddings",
    "rag.indexer",
)


def _packages(path: Path) -> dict[str, str]:
    """返回 {包名: 原始约束行}，忽略注释、空行与 `-r` 引用。"""
    entries: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        name = line.split("[")[0]
        for separator in (">=", "<=", "==", "~=", "!=", ">", "<"):
            name = name.split(separator)[0]
        entries[name.strip().casefold()] = line
    return entries


def test_root_requirements_stay_cloud_deployable():
    root = _packages(PROJECT_ROOT / "requirements.txt")
    offenders = sorted(HEAVY_PACKAGES & set(root))
    assert offenders == [], (
        "requirements.txt 必须保持可直接部署到 Streamlit Community Cloud；"
        f"以下重型依赖应移到 requirements-rag.txt：{offenders}"
    )


def test_optional_requirements_extend_the_root_file():
    text = (PROJECT_ROOT / "requirements-rag.txt").read_text(encoding="utf-8")
    assert "-r requirements.txt" in text, "requirements-rag.txt 必须继承根清单，避免两处各写一半"


def test_optional_requirements_declare_exactly_the_vector_stack():
    optional = set(_packages(PROJECT_ROOT / "requirements-rag.txt"))
    assert optional == OPTIONAL_RAG_PACKAGES, (
        f"requirements-rag.txt 只应声明可选向量栈；当前为 {sorted(optional)}"
    )


def test_every_declared_package_is_pinned_in_the_lock_file():
    lock = _packages(PROJECT_ROOT / "requirements-lock.txt")
    declared = set(_packages(PROJECT_ROOT / "requirements.txt"))
    declared |= set(_packages(PROJECT_ROOT / "requirements-rag.txt"))
    missing = sorted(declared - set(lock))
    assert missing == [], f"以下依赖没有出现在 requirements-lock.txt：{missing}"


def test_pinned_packages_match_the_lock_file_exactly():
    """凡是在根清单里写死 == 的包，必须与 requirements-lock.txt 一致。

    这些 pin 存在的原因通常是某条测试对该库的具体行为有断言；
    与锁定版本脱钩就会出现「本机绿、CI 红」。
    """
    lock = _packages(PROJECT_ROOT / "requirements-lock.txt")
    mismatched: dict[str, tuple[str, str]] = {}
    for name, line in _packages(PROJECT_ROOT / "requirements.txt").items():
        if "==" not in line:
            continue
        pinned = line.split("==", 1)[1].strip()
        locked = lock.get(name, "").split("==", 1)[-1].strip()
        if pinned != locked:
            mismatched[name] = (pinned, locked or "<不在 lock 中>")
    assert not mismatched, f"固定版本与 requirements-lock.txt 不一致：{mismatched}"


def test_pypdf_stays_pinned_because_a_quality_gate_asserts_on_its_output():
    """回归：pypdf 6.18.1 会把乱码 PDF 判成「信息密度过低」而非「编码异常」，
    使 tests/test_pdf_quality_and_value.py 在 CI 上失败。放宽为区间会让 CI 随
    上游发版随机变红。"""
    line = _packages(PROJECT_ROOT / "requirements.txt")["pypdf"]
    assert line.startswith("pypdf=="), f"pypdf 必须固定版本，当前为 {line}"


def test_public_demo_path_imports_without_the_vector_stack():
    """部署保证：缺少 chromadb / sentence-transformers / torch 时仍可导入。

    在子进程中执行，避免污染当前 pytest 会话的 sys.modules。
    """
    script = textwrap.dedent(
        """
        import sys

        BLOCKED = {"chromadb", "sentence_transformers", "torch", "transformers"}

        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in BLOCKED:
                    raise ImportError(f"[simulated deployment] {name} is not installed")
                return None

        sys.meta_path.insert(0, Blocker())
        for module in list(sys.modules):
            if module.split(".")[0] in BLOCKED:
                del sys.modules[module]

        import importlib
        failures = []
        for name in %(modules)r:
            try:
                importlib.import_module(name)
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        print("FAILURES=" + repr(failures))
        """
    ) % {"modules": list(MODULES_THAT_MUST_IMPORT_WITHOUT_VECTOR_STACK)}

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    marker = [line for line in completed.stdout.splitlines() if line.startswith("FAILURES=")]
    assert marker, completed.stdout + completed.stderr
    failures = eval(marker[-1].split("=", 1)[1])  # noqa: S307 - 仅解析本进程刚生成的字面量
    assert failures == [], f"缺少向量栈时以下模块无法导入：{failures}"
