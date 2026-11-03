import pytest

from sanmou_assistant.locations import parse_addresses, single_address


def test_game_share_concatenated_addresses_and_round_trip():
    pasted = "涪县(595,277)测试玩家甲(619,282)测试主公(607,291)山道(636,286)测试玩家乙(634,286)"
    addresses = parse_addresses(pasted)
    assert [(a.name, a.x, a.y) for a in addresses] == [
        ("涪县",595,277),("测试玩家甲",619,282),("测试主公",607,291),("山道",636,286),("测试玩家乙",634,286)]
    assert parse_addresses("\n".join(a.text() for a in addresses)) == addresses
    assert single_address(" 涪县（ 595 ， 277 ） ").text() == "涪县(595,277)"
    assert single_address("(0,3000)").name == ""
    assert len(parse_addresses("涪县(595,277)；涪县(595,277)")) == 1
    assert single_address("营帐（升级后）(100,200)").text() == "营帐（升级后）(100,200)"


@pytest.mark.parametrize("text", ["", "涪县595,277", "涪县(-1,277)", "涪县(3001,277)", "涪县(595,277)尾巴", "涪县(595,277)坏地址(1,)", "涪县(595,277)别的城(595,277)"])
def test_malformed_paste_is_rejected_instead_of_importing_valid_prefix(text):
    with pytest.raises(ValueError):
        parse_addresses(text)
    with pytest.raises(ValueError):
        single_address("甲(1,2)乙(3,4)")
