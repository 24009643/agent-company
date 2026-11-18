"""Step 2.1 装闸自检:require_hr 权限咽喉(I7)——仅 hr 通过,admin 也 403。

依赖函数可直接传 user 调用(绕过 FastAPI 的 Depends 注入),无需起服务器。
运行:python test_auth.py
"""
from fastapi import HTTPException
from app import require_hr, require_rubric_editor


def _denied(role):
    try:
        require_hr(user={"role": role})
        return False
    except HTTPException as e:
        return e.status_code == 403


def main():
    # hr 通过,原样返回 user
    u = {"role": "hr", "username": "hr"}
    assert require_hr(user=u) is u, "hr 应通过并原样返回 user"
    # 评分配置者属于 HR 内容角色，可使用人才库；admin 仍不可见
    editor = {"role": "hr_rubric_editor", "username": "hr_editor"}
    assert require_hr(user=editor) is editor, "评分配置者应具有人才库使用权"
    assert require_rubric_editor(user=editor) is editor, "评分配置者应可配置 rubric"
    # 其余一律 403——admin 也不行(I7:admin 是系统管理权,非内容权;裁决1)
    for role in ("staff", "finance", "admin", "tech"):
        assert _denied(role), f"{role} 应被 require_hr 403 拒绝"
        try:
            require_rubric_editor(user={"role": role})
            assert False, f"{role} 不得配置 rubric"
        except HTTPException as e:
            assert e.status_code == 403
    print("require_hr 自检通过 ✅(仅 hr 过,admin 亦 403)")


if __name__ == "__main__":
    main()
