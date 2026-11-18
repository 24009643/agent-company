"""F5 公平性自检:受保护属性封闭集的减脂与扫描。运行:python test_fairness.py"""
import fairness


def main():
    # 扫描:命中类别名(封闭集可穷举)
    assert set(fairness.scan_protected("候选人32岁,已育")) == {"年龄", "婚育"}, "应命中 年龄+婚育"
    assert "学校档次" in fairness.scan_protected("三本院校毕业,基础偏弱")
    assert "籍贯" in fairness.scan_protected("籍贯河南")
    assert fairness.scan_protected("精通 Python 和 SQL,主导支付系统") == [], "岗位相关内容不应命中"
    # F-A 收窄:闭集不误杀常见词,但真实民族/学历仍命中
    assert fairness.scan_protected("资深上班族,读过一本书") == [], "上班族/一本书不应误命中"
    assert "民族" in fairness.scan_protected("候选人为汉族"), "真实民族应命中"

    # 减脂:多字符可锚定项被隐去,岗位相关内容保留
    red = fairness.redact_protected("张三,32岁,已婚已育,籍贯河南,精通 Python,某三本毕业")
    assert "32岁" not in red and "已婚" not in red and "籍贯河南" not in red and "三本" not in red, f"受保护项应减脂:{red}"
    assert "Python" in red and "张三" in red, f"姓名/技能应保留:{red}"

    print("fairness 自检通过 ✅(封闭集扫描 + 减脂)")


if __name__ == "__main__":
    main()
