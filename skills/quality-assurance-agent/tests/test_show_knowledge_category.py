"""测试 show-knowledge 的 --category 过滤功能"""
import json
import subprocess
from pathlib import Path

# 绝对路径定位 CLI：写成 "scripts/qa_agent.py" 只有 cwd 在技能目录下才跑得通，
# 从仓库根跑 pytest 会直接报 No such file。
AGENT = Path(__file__).resolve().parent.parent / "scripts" / "qa_agent.py"


def test_show_knowledge_category_filter(tmp_path):
    """验证 show-knowledge --category 能正确过滤"""
    # 创建测试知识库
    knowledge_dir = tmp_path / ".qa-agent" / "knowledge"
    knowledge_dir.mkdir(parents=True)

    # 创建 test-module.json，包含多个条目
    test_module_data = [
        {
            "category": "bug-pattern",
            "title": "MySQL 连接池泄漏",
            "description": "未正确关闭连接",
            "module": "test-module",
        },
        {
            "category": "api-quirk",
            "title": "商品接口返回 null",
            "description": "某些情况下返回 null 而非空数组",
            "module": "test-module",
        },
    ]

    (knowledge_dir / "test-module.json").write_text(json.dumps(test_module_data, ensure_ascii=False), encoding="utf-8")

    # 测试不带 --category：应返回所有记录
    result = subprocess.run(
        ["python", str(AGENT), "show-knowledge", "--repo", str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    all_data = json.loads(result.stdout)
    assert "test-module" in all_data
    assert len(all_data["test-module"]) == 2

    # 测试 --category bug-pattern：应只返回 bug-pattern
    result = subprocess.run(
        ["python", str(AGENT), "show-knowledge", "--repo", str(tmp_path), "--category", "bug-pattern"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    filtered = json.loads(result.stdout)
    assert "test-module" in filtered
    assert len(filtered["test-module"]) == 1
    assert filtered["test-module"][0]["category"] == "bug-pattern"

    # 测试 --category api-quirk：应只返回 api-quirk
    result = subprocess.run(
        ["python", str(AGENT), "show-knowledge", "--repo", str(tmp_path), "--category", "api-quirk"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    filtered = json.loads(result.stdout)
    assert "test-module" in filtered
    assert len(filtered["test-module"]) == 1
    assert filtered["test-module"][0]["category"] == "api-quirk"

    # 测试不存在的 category
    result = subprocess.run(
        ["python", str(AGENT), "show-knowledge", "--repo", str(tmp_path), "--category", "nonexistent"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert "未找到分类为 'nonexistent' 的经验记录" in result.stdout


def test_show_knowledge_module_and_category(tmp_path):
    """验证 --module 和 --category 可以组合使用"""
    # 创建多个模块的知识库
    knowledge_dir = tmp_path / ".qa-agent" / "knowledge"
    knowledge_dir.mkdir(parents=True)

    for module in ["module-a", "module-b"]:
        module_data = [
            {"category": "bug-pattern", "title": f"{module} bug", "module": module},
            {"category": "api-quirk", "title": f"{module} api", "module": module},
        ]
        (knowledge_dir / f"{module}.json").write_text(json.dumps(module_data, ensure_ascii=False), encoding="utf-8")

    # 测试 --module module-a --category bug-pattern
    result = subprocess.run(
        [
            "python", str(AGENT), "show-knowledge",
            "--repo", str(tmp_path),
            "--module", "module-a",
            "--category", "bug-pattern",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert "module-a" in data
    assert "module-b" not in data
    assert len(data["module-a"]) == 1
    assert data["module-a"][0]["category"] == "bug-pattern"
