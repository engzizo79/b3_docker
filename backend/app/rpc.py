"""JSON-RPC 2.0 client wrapper for the B3Hive node — the ONLY place that
talks to the node. Every call passes through the allowlist; anything not
explicitly allowed raises RPCNotAllowed (the API layer maps it to 403).

Amounts: the node uses 9-decimal B3 (1 B3 = 1e9 base units). Parsing helpers
use Decimal — floats are never used for money."""

from decimal import Decimal
from urllib.parse import quote

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
        "decoderawtransaction",
        "getblockcount", "getbestblockhash", "getdifficulty",
        "getblockhash", "verifytxoutproof", "validateaddress", "help",
    },
    "finality_bridge_read": {
        "getfinalitystatus", "getbridgeinfo", "getassetstate", "getindexinfo",
    },
    "mempool_read": {"getmempoolinfo", "getrawmempool", "estimatesmartfee"},
    "supply_read": {"gettxoutsetinfo"},
    "wallet_read": {
        "getwalletinfo", "listunspent", "getaddressesbylabel",
        "listaddressgroupings", "listreceivedbyaddress", "listtransactions", "getbalance", "getbalances",
        "getnewaddress", "getaccountaddress", "listlabels",
        # listdescriptors defaults to private=false (xpubs only, no keys —
        # the API layer never passes private=true); deriveaddresses is a
        # pure math utility. Both read-only, no unlock needed.
        "listdescriptors", "deriveaddresses",
    },
    "wallet_write": {  # gated: require unlocked wallet in the API layer
        "walletpassphrase", "walletlock", "createrawtransaction",
        "fundrawtransaction", "signrawtransactionwithwallet",
        "sendrawtransaction", "sendtoaddress", "sendmany",
 "testmempoolaccept", "startstaking",
        "stopstaking", "signmessage", "createstake", "sendall",
        "bindfinalitykey", "revokefinalitykey",
    },
    "wallet_messaging": { "setlabel", "verifymessage", "gettransaction" },
	"wallet_security": { "walletpassphrasechange" },
	"network_read": {"getpeerinfo", "stop"},
	"staking_read": {"getstakinginfo", "getfinalityinfo"},
    "assets_read": {
        "getwalletassets", "listflowmeshmarkets", "getflowmeshmarketdata",
    },
    "assets_write": {  # gated: require unlocked wallet in the API layer
        "startflowmeshvalidator", "stopflowmeshvalidator", "createfncoin",
    },
    "wallet_lifecycle": {"createwallet", "loadwallet", "listwallets", "listwalletdir", "unloadwallet", "backupwallet"},
}

ALLOWED_METHODS: set[str] = set().union(*ALLOWED.values())

# Explicitly forbidden even if someone adds them to ALLOWED above — defense
# in depth against future edits.
FORBIDDEN: set[str] = {
    "importprivkey", "importmulti", "dumpprivkey", "dumpwallet",
    "encryptwallet", "signmessagewithprivkey", "setgenerate",
    "generate", "generatetoaddress", "importaddress", "importpubkey",
    "dumpwallet", "setaccount",
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
    """Async JSON-RPC client with allowlist choke point.

    Wallet routing: every call() / call_optional() / call_unrestricted()
    takes an optional `wallet` kwarg that targets Bitcoin-Core-style
    per-wallet RPC (POST /wallet/<name> instead of POST /) — the same
    mechanism the node itself already uses for its own multiwallet
    support. wallet=None (every call site today) is unchanged behavior:
    the bare endpoint, exactly as before this existed.

    This is plumbing only, laid down ahead of any decision to actually
    build per-user wallet isolation (see docs/MULTINODE_PLAN.md-adjacent
    discussion) — nothing yet resolves a logged-in user to a wallet name.
    It exists so that if/when that's built, the RPC layer doesn't need to
    change: only the (as yet nonexistent) code that decides which wallet
    a request is for needs to start passing this kwarg."""

    def __init__(self, host: str, port: int, user: str, password: str,
                 timeout: float = 30.0):
        self._base_url = f"http://{host}:{port}/"
        self._auth = (user, password)
        self._timeout = timeout

    def _url_for(self, wallet: str | None) -> str:
        if not wallet:
            return self._base_url
        # Matches the node's own URI parsing (src/wallet/rpc/util.cpp
        # GetWalletNameFromJSONRPCRequest): "/wallet/" + url-decoded name,
        # no trailing slash.
        return self._base_url + "wallet/" + quote(wallet, safe="")

    async def _post(self, method: str, params: tuple, wallet: str | None) -> object:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method,
                   "params": list(params)}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._url_for(wallet), json=payload, auth=self._auth)
        except httpx.HTTPError as exc:
            raise RPCUnavailable(str(exc)) from exc
        if resp.status_code == 401:
            raise RPCError(-1, "node RPC rejected credentials")
        data = resp.json()
        if "error" in data and data["error"] is not None:
            raise RPCError(data["error"].get("code", -1),
                           data["error"].get("message", "unknown error"))
        return data.get("result")

    async def call(self, method: str, *params, wallet: str | None = None) -> object:
        assert_allowed(method)  # hard choke point — no path around it
        return await self._post(method, params, wallet)

    async def call_optional(self, method: str, *params,
                            wallet: str | None = None) -> object | None:
        """Like call() but returns None when the method is not allowlisted
        (used for optional features like staking RPCs on older nodes)."""
        try:
            assert_allowed(method)
        except RPCNotAllowed:
            return None
        return await self.call(method, *params, wallet=wallet)

    async def call_unrestricted(self, method: str, *params,
                                wallet: str | None = None) -> object:
        """Raw RPC call WITHOUT the allowlist choke point.

        Used ONLY by the expert console in full-trust mode (localhost /
        operator-allowlisted networks), where the operator has explicitly
        chosen QT-parity: allow everything, warn but never prohibit.
        Every other code path MUST use call() / call_optional() so the
        allowlist stays the hard boundary for the regular API surface.
        """
        return await self._post(method, params, wallet)
