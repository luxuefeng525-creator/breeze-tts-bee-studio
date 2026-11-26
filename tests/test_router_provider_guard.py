"""收口修补的回归锁（G3 RV-03 / RV-08）：

1. 未知 provider 不能冒泡成 KeyError → 500，必须是人话 400（ProviderError）
2. 角色参考图只接受 /api/refs/ 下的引用，不给「整机任意路径」留口子
"""
import pytest

from api.providers import ProviderError
from api.router import _make_provider, _ref_image_local


def test_make_provider_rejects_unknown():
    with pytest.raises(ProviderError) as ei:
        _make_provider("not_a_provider")
    assert "未知的出片方案" in str(ei.value)


def test_ref_image_local_rejects_arbitrary_path(tmp_path):
    role = {"name": "x", "ref_image": str(tmp_path / "any.png")}
    with pytest.raises(ProviderError) as ei:
        _ref_image_local(role)
    assert "非法的参考图引用" in str(ei.value)


def test_ref_image_local_missing_file_is_rejected(tmp_path, monkeypatch):
    """形式合法但文件不在（悬空引用）同样要拒绝。"""
    monkeypatch.setenv("BREEZE_DATA", str(tmp_path))
    role = {"name": "x", "ref_image": "/api/refs/characters/r_x/none.png"}
    with pytest.raises(ProviderError) as ei:
        _ref_image_local(role)
    assert "不存在" in str(ei.value)
