"""JSON-RPC 2.0 client wrapper for the B3Hive node — the ONLY place that
talks to the node. Every call passes through the allowlist; anything not
explicitly allowed raises RPCNotAllowed (the API layer maps it to 403).

Amounts: the node uses 9-decimal B3 (1 B3 = 1e9 base units). Parsing helpers
use Decimal — floats are never used for money."""

from decimal import Decimal

import httpx


class RPCError(Exception):
    """The node returned a JSON-RPC error."""

    def __init__(self, code: int, message: str):
        super().__init__(f"RPC {code}: {message}")
        self.code = code
        self.message = message


class RPCNotAllowed(Exception):
    """The RPC method is not on the allowlist."""


class RPCUnavailable(Exception):
    """The node could not be reached."""


# ---------------------------------------------------------------------------
# Allowlist — single source of truth. Categories are documentation only;
# enforcement is membership in ALLOWED.
# ---------------------------------------------------------------------------
ALLOWED: dict[str, set[str]] = {
    "chain_read": {
        "getblockchaininfo", "getnetworkinfo", "getblock", "getblockheader",
        "getblockstats", "gettxout", "gettxoutproof", "getrawtransaction",
        "getblockcount", "getbestblockhash", "getdifficulty",
    },
    "finality_bridge_read": {
        "getfinalitystatus", "getbridgeinfo", "getassetstate", "getindexinfo",
    },
    "mempool_read": {"getmempoolinfo", "getrawmempool", "estimatesmartfee"},
    "supply_read": {"gettxoutsetinfo"},
    "wallet_read": {
        "getwalletinfo", "listunspent", "getaddressesbylabel",
        "listaddressgroupings", "listtransactions", "getbalance", "getbalances",
        "getnewaddress", "getaccountaddress", "listlabels",
    },
    "wallet_write": {  # gated: require unlocked wallet in the API layer
        "walletpassphrase", "walletlock", "createrawtransaction",
        "fundrawtransaction", "signrawtransactionwithwallet",
        "sendrawtransaction", "testmempoolaccept", "startstaking",
        "stopstaking", "signmessage", "createstake", "sendall",
    },
    "wallet_messaging": { "setlabel", "verifymessage", "gettransaction" },
	"wallet_security": { "walletpassphrasechange" },
	"network_read": {"getpeerinfo"},
	"staking_read": {"getstakinginfo"},
    "assets_read": {
        "getwalletassets", "listflowmeshmarkets", "getflowmeshmarketdata",
    },
    "assets_write": {  # gated: require unlocked wallet in the API layer
        "startflowmeshvalidator", "stopflowmeshvalidator",
    },
    "wallet_lifecycle": {"createwallet", "loadwallet", "listwallets", "unloadwallet"},
}

ALLOWED_METHODS: set[str] = set().union(*ALLOWED.values())

# Explicitly forbidden even if someone adds them to ALLOWED above — defense
# in depth against future edits.
FORBIDDEN: set[str] = {
    "importprivkey", "importmulti", "dumpprivkey", "dumpwallet",
    "encryptwallet", "signmessagewithprivkey", "setgenerate",
    "generate", "generatetoaddress", "importaddress", "importpubkey",
    "dumpwallet", "backupwallet", "setaccount",
}


def assert_allowed(method: str) -> None:
    """Raise RPCNotAllowed unless the method is allowlisted and not forbidden."""
    if method in FORBIDDEN or method not in ALLOWED_METHODS:
        raise RPCNotAllowed(method)


# ---------------------------------------------------------------------------
# B3 money helpers (9 decimals) — Decimal only, never float.
# ---------------------------------------------------------------------------
B3_DECIMALS = 9


def to_base_units(amount: Decimal) -> int:
    """B3 -> base units (int). 1 B3 = 1e9 base units."""
    return int((amount * (10 ** B3_DECIMALS)).to_integral_value())


def from_base_units(base: int) -> Decimal:
    """base units (int) -> B3 Decimal with 9dp."""
    return Decimal(base) / (10 ** B3_DECIMALS)


def parse_amount(value: str) -> Decimal:
    """Parse a user-supplied amount string; reject negatives and >9dp."""
    text = str(value).strip()
    if 'e' in text.lower():
        raise ValueError("scientific notation is not accepted")
    try:
        amount = Decimal(text)
    except ArithmeticError as exc:
        raise ValueError(f"invalid amount: {value}") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be a positive finite number")
    if -amount.as_tuple().exponent > B3_DECIMALS:
        raise ValueError("B3 amounts have at most 9 decimals")
    return amount


class B3RPCClient:
    """Async JSON-RPC client with allowlist choke point."""

    def __init__(self, host: str, port: int, user: str, password: str,
                 timeout: float = 30.0):
        self._url = f"http://{host}:{port}/"
        self._auth = (user, password)
        self._timeout = timeout

    async def call(self, method: str, *params) -> object:
        assert_allowed(method)  # hard choke point — no path around it
        payload = {"jsonrpc": "2.0", "id": 1, "method": method,
                   "params": list(params)}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._url, json=payload, auth=self._auth)
        except httpx.HTTPError as exc:
            raise RPCUnavailable(str(exc)) from exc
        if resp.status_code == 401:
            raise RPCError(-1, "node RPC rejected credentials")
        data = resp.json()
        if "error" in data and data["error"] is not None:
            raise RPCError(data["error"].get("code", -1),
                           data["error"].get("message", "unknown error"))
        return data.get("result")

    async def call_optional(self, method: str, *params) -> object | None:
        """Like call() but returns None when the method is not allowlisted
        (used for optional features like staking RPCs on older nodes)."""
        try:
            assert_allowed(method)
        except RPCNotAllowed:
            return None
        return await self.call(method, *params)
