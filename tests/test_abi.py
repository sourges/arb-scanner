import unittest

from eth_abi import decode, encode

import abi


class TestAbi(unittest.TestCase):
    def test_selectors_match_published_values(self):
        # Known 4-byte selectors (Multicall3, Uniswap v3 factory/QuoterV2, ERC-20)
        self.assertEqual(abi.selector(abi.SIG_AGGREGATE3).hex(), "82ad56cb")
        self.assertEqual(abi.selector(abi.SIG_GET_POOL_UNI).hex(), "1698ee82")
        self.assertEqual(abi.selector(abi.SIG_QUOTE_UNI).hex(), "c6a5026a")
        self.assertEqual(abi.selector(abi.SIG_SYMBOL).hex(), "95d89b41")
        self.assertEqual(abi.selector(abi.SIG_DECIMALS).hex(), "313ce567")
        self.assertEqual(abi.selector(abi.SIG_BASEFEE).hex(), "3e64a696")
        self.assertEqual(abi.selector(abi.SIG_BLOCK_NUMBER).hex(), "42cbb15c")

    def test_quote_struct_field_order(self):
        tin, tout = "0x" + "11" * 20, "0x" + "22" * 20
        for kind, tup in (("uni", "uint24"), ("slip", "int24")):
            data = abi.encode_quote(kind, tin, tout, 123_456, 500)
            ((a, b, amount, tier, limit),) = decode([f"(address,address,uint256,{tup},uint160)"], data[4:])
            self.assertEqual((a, b, amount, tier, limit), (tin, tout, 123_456, 500, 0))

    def test_slip_get_pool_encodes_int24_tick_spacing(self):
        data = abi.encode_get_pool("slip", "0x" + "11" * 20, "0x" + "22" * 20, 100)
        self.assertEqual(data[:4], abi.selector(abi.SIG_GET_POOL_SLIP))
        self.assertEqual(decode(["address", "address", "int24"], data[4:])[2], 100)

    def test_aggregate3_round_trip(self):
        calls = [("0x" + "aa" * 20, b"\x01\x02"), ("0x" + "bb" * 20, b"")]
        (decoded,) = decode(["(address,bool,bytes)[]"], abi.encode_aggregate3(calls)[4:])
        self.assertEqual([(t, ok, bytes(d)) for t, ok, d in decoded],
                         [("0x" + "aa" * 20, True, b"\x01\x02"), ("0x" + "bb" * 20, True, b"")])
        ret = encode(["(bool,bytes)[]"], [[(True, b"\x05"), (False, b"")]])
        self.assertEqual(abi.decode_aggregate3(ret), [(True, b"\x05"), (False, b"")])

    def test_decode_quote_returns_amount_and_gas(self):
        ret = encode(["uint256", "uint160", "uint32", "uint256"], [999, 1, 2, 88_000])
        self.assertEqual(abi.decode_quote(ret), (999, 88_000))

    def test_mixed_case_addresses_accepted(self):
        # checksummed addresses straight from config must encode without errors
        abi.encode_get_pool("uni", "0x4200000000000000000000000000000000000006",
                            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 500)


if __name__ == "__main__":
    unittest.main()
