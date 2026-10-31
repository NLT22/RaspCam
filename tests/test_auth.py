from raspcam.auth import hash_password, verify_password


def test_roundtrip():
    stored = hash_password("letmein")
    assert stored.startswith("scrypt$")
    assert verify_password("letmein", stored)
    assert not verify_password("wrong", stored)


def test_salt_makes_hashes_differ():
    assert hash_password("same") != hash_password("same")


def test_malformed_hash_is_rejected_without_raising():
    for bad in ["", "scrypt$x", "bcrypt$1$2$3$c2FsdA==$aGFzaA==", "scrypt$a$b$c$!!$!!"]:
        assert verify_password("pw", bad) is False
