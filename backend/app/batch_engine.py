"""User-definable batch-action engine.

Recipes are declarative JSON (never code). The engine interprets recipes
and enforces hard safety rails REGARDLESS of recipe content:

- ONLY plain P2PKH outputs (script 76a914<h160>88ac) are ever selected.
  B3S1 stake carriers, B3A1 colored-asset envelopes and B3MC metadata
  cells are skipped unconditionally - spending them would unstake the
  principal or move colored assets. This rail is not configurable.
- spendable-only UTXOs; Decimal 9dp money (floats never touch amounts).
- fee rate clamped UP to the node's minrelaytxfee floor.
- inputs_per_tx hard-capped at 675 (B3 MAX_STANDARD_TX_WEIGHT policy).
- every broadcast is gated by testmempoolaccept; the executor aborts on
  the first rejection; every run writes an audit-log entry.

Patterns reused from the verified b3-utxo-combiner (b3_monitoring).
"""

import hashlib
import hmac
import re
from decimal import ROUND_DOWN, ROUND_UP, Decimal, getcontext

getcontext().prec = 34

Q9 = Decimal(10) ** -9
P2PKH_SCRIPT_RE = re.compile(r"^76a914[0-9a-f]{40}88ac$")
B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_MAP = {c: i for i, c in enumerate(B58_ALPHABET)}
B3_P2PKH_VERSION = 0x3F

TX_OVERHEAD_VBYTES = 10
P2PKH_INPUT_VBYTES = 148
P2PKH_OUTPUT_VBYTES = 34
HARD_MAX_INPUTS = (100_000 - TX_OVERHEAD_VBYTES - P2PKH_OUTPUT_VBYTES) // P2PKH_INPUT_VBYTES  # 675
MAX_BATCHES_CAP = 25

DEFAULT_FEE_RATE = Decimal("0.0001")
DEFAULT_MIN_OUTPUT = Decimal("0.0001")
DEFAULT_MIN_RELAY_RATE = Decimal("0.0000001")
VALID_SORTS = {"smallest", "largest", "oldest"}


class RecipeError(ValueError):
    """Recipe failed validation (user error, maps to 400/422)."""


def _b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        if c not in B58_MAP:
            raise RecipeError(f"invalid base58 character: {c!r}")
        n = n * 58 + B58_MAP[c]
    pad = 0
    for c in s:
        if c == B58_ALPHABET[0]:
            pad += 1
        else:
            break
    body = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    return b"" * pad + body


def validate_b3_address(addr: str) -> bool:
    """Base58Check + version byte 0x3F (S prefix), 21-byte payload."""
    if not isinstance(addr, str) or not addr:
        return False
    try:
        raw = _b58decode(addr)
    except RecipeError:
        return False
    if len(raw) < 5:
        return False
    payload, checksum = raw[:-4], raw[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != expected:
        return False
    return len(payload) == 21 and payload[0] == B3_P2PKH_VERSION


def _dec(value, default: str) -> Decimal:
    """Parse a non-negative Decimal from user input (None -> default)."""
    if value is None or value == "":
        value = default
    try:
        d = Decimal(str(value))
    except ArithmeticError as exc:
        raise RecipeError(f"invalid number: {value}") from exc
    if not d.is_finite() or d < 0:
        raise RecipeError(f"must be a non-negative number: {value}")
    return d


def _int(value, default: int, lo: int, hi: int) -> int:
    """Parse an int in [lo, hi] from user input (None -> default)."""
    if value is None or value == "":
        value = default
    try:
        i = int(value)
    except (TypeError, ValueError) as exc:
        raise RecipeError(f"must be an integer: {value}") from exc
    if not lo <= i <= hi:
        raise RecipeError(f"must be between {lo} and {hi}: {value}")
    return i


def validate_recipe(data: dict) -> dict:
    """Validate and normalize a recipe. Raises RecipeError on bad input.
    Everything user-supplied is checked here; nothing in a recipe can
    weaken the engine rails."""
    if not isinstance(data, dict):
        raise RecipeError("recipe must be a JSON object")

    name = str(data.get("name", "")).strip()
    if not 1 <= len(name) <= 64:
        raise RecipeError("name must be 1-64 characters")

    filters = data.get("filters") or {}
    sources = filters.get("sources")
    if not isinstance(sources, list) or not sources:
        raise RecipeError("filters.sources must be a non-empty list")
    if len(sources) > 50:
        raise RecipeError("too many sources (max 50)")
    srcs = []
    for s in sources:
        s = str(s).strip()
        if not s:
            continue
        if s.lower().startswith("label:"):
            label = s[6:].strip()
            if not label:
                raise RecipeError("empty label in sources")
            srcs.append({"kind": "label", "value": label})
        elif validate_b3_address(s):
            srcs.append({"kind": "address", "value": s})
        else:
            raise RecipeError("invalid B3 P2PKH address in sources")
    if not srcs:
        raise RecipeError("no valid sources")

    sort = str(filters.get("sort") or "smallest")
    if sort not in VALID_SORTS:
        raise RecipeError("sort must be smallest, largest or oldest")

    action = data.get("action") or {}
    if str(action.get("type") or "consolidate") != "consolidate":
        raise RecipeError("action.type must be consolidate")
    destination = str(action.get("destination") or "").strip()
    if not validate_b3_address(destination):
        raise RecipeError("action.destination must be a valid B3 P2PKH address")

    limits = data.get("limits") or {}
    fees = data.get("fees") or {}
    fee_mode = str(fees.get("mode") or "estimate")
    if fee_mode not in ("estimate", "fixed"):
        raise RecipeError("fees.mode must be estimate or fixed")

    return {
        "name": name,
        "filters": {
            "sources": srcs,
            "min_utxo_value": str(_dec(filters.get("min_utxo_value"), "0")),
            "min_conf": _int(filters.get("min_conf"), 1, 1, 9_999_999),
            "max_conf": _int(filters.get("max_conf"), 9_999_999, 1, 9_999_999),
            "sort": sort,
        },
        "action": {"type": "consolidate", "destination": destination},
        "limits": {
            "inputs_per_tx": _int(limits.get("inputs_per_tx"), 50, 1, HARD_MAX_INPUTS),
            "max_batches": _int(limits.get("max_batches"), 5, 1, MAX_BATCHES_CAP),
            "min_output": str(_dec(limits.get("min_output"), str(DEFAULT_MIN_OUTPUT))),
        },
        "fees": {
            "mode": fee_mode,
            "fee_rate": str(_dec(fees.get("fee_rate"), str(DEFAULT_FEE_RATE))),
            "fee_target": _int(fees.get("fee_target"), 6, 1, 100),
            "fallback_fee_rate": str(_dec(fees.get("fallback_fee_rate"), str(DEFAULT_FEE_RATE))),
        },
    }
def vsize(n_inputs: int, n_outputs: int = 1) -> int:
    return TX_OVERHEAD_VBYTES + n_inputs * P2PKH_INPUT_VBYTES + n_outputs * P2PKH_OUTPUT_VBYTES


def compute_fee(n_inputs: int, fee_rate: Decimal) -> Decimal:
    """Fee for n inputs, rounded UP to 9dp (never underpay)."""
    return (Decimal(vsize(n_inputs)) * fee_rate / Decimal(1000)).quantize(Q9, ROUND_UP)


def confirm_token(recipe_id: int, plan_key: str, secret: str) -> str:
    """HMAC binding a confirm to one exact plan (recipe id + batch shape).
    Execute recomputes and compares, so a confirm can never replay
    against a different UTXO set."""
    return hmac.new(secret.encode(), f"{recipe_id}:{plan_key}".encode(),
                    hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Planning engine (pure reads: no signing, no broadcasting happens here)
# ---------------------------------------------------------------------------

class BatchEngine:
    """Reads the wallet via the allowlisted RPC client and builds plans.
    The executor in routers/batch.py does signing/broadcast behind the
    unlock + CSRF + confirm-token gates."""

    def __init__(self, rpc):
        self.rpc = rpc  # B3RPCClient (or mock) - allowlist choke point applies

    async def resolve_sources(self, recipe: dict) -> list[str]:
        """Resolve recipe sources (addresses and label:NAME) to addresses."""
        addresses: set[str] = set()
        for s in recipe["filters"]["sources"]:
            if s["kind"] == "address":
                addresses.add(s["value"])
                continue
            result = await self.rpc.call("getaddressesbylabel", s["value"])
            if not isinstance(result, dict):
                raise RecipeError(f"cannot resolve label {s['value']!r}")
            for addr in result:
                if validate_b3_address(addr):
                    addresses.add(addr)
        if not addresses:
            raise RecipeError("no source addresses resolved")
        return sorted(addresses)

    async def fetch_utxos(self, recipe: dict, addresses: list[str]) -> tuple[list, dict]:
        """List unspent for the sources; keep ONLY spendable plain-P2PKH
        outputs at or above min_utxo_value. Carriers are ALWAYS skipped
        (B3S1 stake carriers, B3A1 envelopes, B3MC cells - never spent)."""
        f = recipe["filters"]
        utxos = await self.rpc.call(
            "listunspent", int(f["min_conf"]), int(f["max_conf"]), addresses)
        if not isinstance(utxos, list):
            raise RecipeError("unexpected listunspent response")
        min_val = Decimal(f["min_utxo_value"])
        kept: list[dict] = []
        skipped = {"script": 0, "unspendable": 0, "value": 0}
        for u in utxos:
            if not u.get("spendable", False):
                skipped["unspendable"] += 1
                continue
            spk = str(u.get("scriptPubKey", "")).lower()
            if not P2PKH_SCRIPT_RE.match(spk):
                skipped["script"] += 1  # carriers / envelopes / anything exotic
                continue
            val = u["amount"] if isinstance(u["amount"], Decimal) else Decimal(str(u["amount"]))
            if not val.is_finite() or val <= 0 or val < min_val:
                skipped["value"] += 1
                continue
            kept.append({
                "txid": u["txid"], "vout": int(u["vout"]),
                "address": u.get("address", ""), "amount": val,
                "confirmations": int(u.get("confirmations", 0)),
            })
        sort = f["sort"]
        if sort == "largest":
            kept.sort(key=lambda x: x["amount"], reverse=True)
        elif sort == "oldest":
            kept.sort(key=lambda x: x["confirmations"], reverse=True)
        else:
            kept.sort(key=lambda x: x["amount"])
        return kept, skipped

    async def fee_rate(self, recipe: dict) -> tuple[Decimal, str]:
        """Resolve the fee rate and clamp it UP to the relay floor."""
        fees = recipe["fees"]
        rate: Decimal | None = None
        source = ""
        if fees["mode"] == "fixed":
            rate = Decimal(fees["fee_rate"])
            source = "fixed"
        else:
            try:
                result = await self.rpc.call("estimatesmartfee", int(fees["fee_target"]))
                rate = Decimal(str(result["feerate"]))
                source = f"estimatesmartfee({fees['fee_target']})"
            except Exception:
                rate = Decimal(fees["fallback_fee_rate"])
                source = "fallback"
        floor = DEFAULT_MIN_RELAY_RATE
        try:
            info = await self.rpc.call("getmempoolinfo")
            mr = info.get("minrelaytxfee") if isinstance(info, dict) else None
            if mr is not None:
                candidate = mr if isinstance(mr, Decimal) else Decimal(str(mr))
                if candidate > 0:
                    floor = candidate
        except Exception:
            pass
        if rate < floor:
            rate = floor
            source += " (clamped to relay floor)"
        return rate, source

    async def plan(self, recipe_id: int, recipe: dict) -> dict:
        """Build the full plan WITHOUT signing anything. Internal: also
        returns the Decimal fee rate and the exact UTXO chunks (for the
        executor only - never serialized to the client)."""
        addresses = await self.resolve_sources(recipe)
        utxos, skipped = await self.fetch_utxos(recipe, addresses)
        rate, fee_source = await self.fee_rate(recipe)
        limits = recipe["limits"]
        per = int(limits["inputs_per_tx"])
        chunks = [utxos[i:i + per] for i in range(0, len(utxos), per)][: int(limits["max_batches"])]
        batches = []
        for chunk in chunks:
            total_in = sum((u["amount"] for u in chunk), Decimal(0))
            fee = compute_fee(len(chunk), rate)
            out = (total_in - fee).quantize(Q9, ROUND_DOWN)
            batches.append({
                "inputs": len(chunk),
                "total_input": str(total_in),
                "fee": str(fee),
                "output": str(out),
                "below_min_output": out < Decimal(limits["min_output"]),
            })
        # plan_key binds the exact UTXO set (txid:vout:amount per input),
        # fee rate and destination into the confirm token. Any change to
        # the UTXO set, fee or destination invalidates the token.
        hasher = hashlib.sha256()
        for chunk in chunks:
            for u in chunk:
                hasher.update(f"{u['txid']}:{u['vout']}:{u['amount']}".encode())
        hasher.update(str(rate).encode())
        hasher.update(recipe["action"]["destination"].encode())
        plan_key = hasher.hexdigest()
        total_output = sum(
            (Decimal(b["output"]) for b in batches if not b["below_min_output"]), Decimal(0))
        total_fee = sum((Decimal(b["fee"]) for b in batches), Decimal(0))
        return {
            "recipe_name": recipe["name"],
            "destination": recipe["action"]["destination"],
            "source_addresses": addresses,
            "eligible_utxos": len(utxos),
            "skipped": skipped,
            "fee_rate": str(rate),
            "fee_source": fee_source,
            "batches": batches,
            "total_output": str(total_output),
            "total_fee": str(total_fee),
            # internal fields - the executor uses these; preview() strips them
            "_rate": rate,
            "_chunks": chunks,
            "_plan_key": plan_key,
        }

    async def preview(self, recipe_id: int, recipe: dict, secret: str) -> dict:
        """User-facing preview: the plan without internal fields, plus a
        confirm token bound to this exact plan shape."""
        p = await self.plan(recipe_id, recipe)
        plan_key = p.pop("_plan_key")
        p.pop("_rate")
        p.pop("_chunks")
        p["confirm_token"] = confirm_token(recipe_id, plan_key, secret)
        return p