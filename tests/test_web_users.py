from web import users
from web.server import is_loopback


def test_password_check_and_roles():
    db = {"amy": users.make_user("correct horse 1", "admin")}
    assert users.check(db, "amy", "correct horse 1") == "admin"
    assert users.check(db, "amy", "wrong password") is None
    assert users.check(db, "nobody", "correct horse 1") is None


def test_lan_needs_accounts():
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and not is_loopback("0.0.0.0")
