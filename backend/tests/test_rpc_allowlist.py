"""Allowlist choke-point and Decimal money-math unit tests."""

from decimal import Decimal

import pytest

from app.rpc import (ALLOWED_METHODS, FORBIDDEN, RPCNotAllowed,
                     assert_allowed, from_base_units, parse_amount,
                     to_base_units)


def test_allowlist_categories_covered():
    for method in ("getblockchaininfo", "getfinalitystatus", "listunspent",
                  "walletpassphrase", "sendrawtransaction", "startstaking",
                  "loadwallet", "getstakinginfo"):
        assert method in ALLOWED_METHODS, method


def test_key_exfiltration_rpcs_blocked():
    for method in FORBIDDEN:
        with pytest.raises(RPCNotAllowed):
            assert_allowed(method)


def test_unknown_rpc_blocked():
    for method in ("dumpprivkey", "stop", "invalidateblock",
                  "whatever", "", "getwalletinfo\u0000"):
        with pytest.raises(RPCNotAllowed):
            assert_allowed(method)


def test_forbidden_not_in_allowlist():
    # Defense in depth: FORBIDDEN entries can never sneak into ALLOWED.
    assert not (FORBIDDEN & ALLOWED_METHODS)


def test_amount_roundtrip():
    assert to_base_units(Decimal("1")) == 10 ** 9
    assert from_base_units(10 ** 9) == Decimal("1")
    assert from_base_units(123456789) == Decimal("0.123456789")
    assert to_base_units(Decimal("0.123456789")) == 123456789


def test_parse_amount_rejects_bad():
    for bad in ("-1", "0", "0.0000000001", "abc", "NaN", "Infinity"):
        with pytest.raises((ValueError, ArithmeticError)):
            parse_amount(bad)


def test_parse_amount_accepts_good():
    assert parse_amount("1") == Decimal("1")
    assert parse_amount("1.000000000") == Decimal("1.000000000")
    assert parse_amount("0.123456789") == Decimal("0.123456789")
