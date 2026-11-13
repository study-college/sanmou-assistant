import pytest

from sanmou_assistant.locations import parse_addresses, single_address


def test_game_share_concatenated_addresses_and_round_trip():
    pasted = "甲城(100,100)乙城(200,200)丙城(300,300)丁城(400,400)戊城(500,500)"
    addresses = parse_addresses(pasted)
    assert [(a.name, a.x, a.y) for a in addresses] == [
        ("甲城",100,100),("乙城",200,200),("丙城",300,300),("丁城",400,400),("戊城",500,500)]
    assert parse_addresses("\n".join(a.text() for a in addresses)) == addresses
    assert single_address(" 甲城（ 100 ， 100 ） ").text() == "甲城(100,100)"
    assert single_address("(0,3000)").name == ""
    assert len(parse_addresses("甲城(100,100)；甲城(100,100)")) == 1
    assert single_address("营帐（升级后）(100,200)").text() == "营帐（升级后）(100,200)"


@pytest.mark.parametrize("text", ["", "甲城100,100", "甲城(-1,277)", "甲城(3001,277)", "甲城(100,100)尾巴", "甲城(100,100)坏地址(1,)", "甲城(100,100)别的城(100,100)"])
def test_malformed_paste_is_rejected_instead_of_importing_valid_prefix(text):
    with pytest.raises(ValueError):
        parse_addresses(text)
    with pytest.raises(ValueError):
        single_address("甲(1,2)乙(3,4)")
