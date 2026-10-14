from ledger_room import ubn


def test_stdnum_examples():
    # python-stdnum doctest examples
    assert ubn.is_valid_new("00501503")
    assert not ubn.is_valid_new("00501502")
    # 7th digit is 7: S=39, S+1=40
    assert ubn.weighted_sum("12345675") == 39
    assert ubn.is_valid_new("12345675")
    assert ubn.is_valid_old("12345675")
    assert not ubn.is_valid_new("12345678")


def test_new_rule_is_superset_of_old():
    import random

    rnd = random.Random(0)
    for _ in range(5000):
        n = f"{rnd.randrange(10**8):08d}"
        if ubn.is_valid_old(n):
            assert ubn.is_valid_new(n), n


def test_new_only_numbers_exist():
    # a number with S % 10 == 5 passes new rule but fails old rule
    found = [f"{i:08d}" for i in range(10**6, 10**6 + 2000)
             if ubn.is_valid_new(f"{i:08d}") and not ubn.is_valid_old(f"{i:08d}")]
    assert found
    c = ubn.check(found[0])
    assert c.valid_new and not c.valid_old
    assert "legacy" in c.explanation


def test_malformed():
    c = ubn.check("1234567")
    assert not c.well_formed and not c.valid_new
    c = ubn.check("1234567A")
    assert not c.well_formed
    c = ubn.check(None)
    assert not c.well_formed


def test_make_valid():
    for n in ubn.make_valid("5201314"):
        assert ubn.is_valid_new(n)
    assert len(ubn.make_valid("5201314")) >= 1
