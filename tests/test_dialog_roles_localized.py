# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 George Saliba <george.saliba@salitronic.com>
"""Chinese Windows captions must carry the same roles as English ones.

Windows localizes every MessageBox button: OKCancel arrives as
"确定"/"取消" and YesNo as "是"/"否". The driver's role table was
written from English and Spanish Altium, so on a zh-CN host it
recognised no button at all. A cancel intent could not dismiss an
unknown dialog, and a confirmation could not be answered, which is
what the live-window tests reported as "a dry run recorded a real
action: stopped".

These assertions need no windows, so they also run on a host where
the live tests skip.
"""

from __future__ import annotations

from eda_agent.ui.dialog_driver import is_answerable_confirmation, role_of
from eda_agent.ui.dialog_report import classify


def test_chinese_buttons_have_roles():
    assert role_of("确定") == "advance"
    assert role_of("是") == "advance"
    assert role_of("下一步 >") == "advance"
    assert role_of("取消") == "dismiss"
    assert role_of("否") == "dismiss"
    assert role_of("关闭") == "dismiss"
    assert role_of("应用") == "commit"
    assert role_of("验证") == "validate"


def test_chinese_question_is_a_confirmation():
    assert classify("确认", ["是否继续？"]) == "confirm"


def test_chinese_failure_is_an_error():
    assert classify("提示", ["操作失败"]) == "error"


def test_chinese_warning_is_a_warning():
    assert classify("提示", ["警告：文件已存在"]) == "warning"


def test_chinese_destructive_confirmation_is_not_answerable():
    dialog = {"title": "确认删除", "message": ["是否删除这 12 个对象？"],
              "ocr_text": []}
    assert not is_answerable_confirmation(dialog)


def test_chinese_information_with_nothing_to_do_is_dismissible():
    assert classify("信息", ["无需更新"]) == "nothing_to_do"
