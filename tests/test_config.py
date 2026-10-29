"""Tests for scripts/config.py — code validation and utilities."""

import pytest

from config import validate_stock_code, split_code


class TestValidateStockCode:
    def test_sh_suffix(self):
        assert validate_stock_code("600887.SH") == "600887.SH"

    def test_sz_suffix(self):
        assert validate_stock_code("000858.SZ") == "000858.SZ"

    def test_plain_6_sh(self):
        assert validate_stock_code("600887") == "600887.SH"

    def test_plain_0_sz(self):
        assert validate_stock_code("000858") == "000858.SZ"

    def test_plain_3_sz(self):
        assert validate_stock_code("300750") == "300750.SZ"

    def test_lowercase_normalized(self):
        assert validate_stock_code("600887.sh") == "600887.SH"

    def test_whitespace_stripped(self):
        assert validate_stock_code("  600887 ") == "600887.SH"

    def test_invalid_prefix_rejected(self):
        with pytest.raises(ValueError):
            validate_stock_code("400001")

    def test_hk_rejected_mvp(self):
        with pytest.raises(ValueError):
            validate_stock_code("00700.HK")

    def test_us_rejected_mvp(self):
        with pytest.raises(ValueError):
            validate_stock_code("AAPL")

    def test_garbage_rejected(self):
        with pytest.raises(ValueError):
            validate_stock_code("伊利股份")


class TestSplitCode:
    def test_sh(self):
        assert split_code("600887.SH") == ("600887", "sh")

    def test_sz(self):
        assert split_code("000858.SZ") == ("000858", "sz")
